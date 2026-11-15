"""公式解析与归一化（纯函数模块，路由与图算法都不依赖 FastAPI）。

支持的语法：
    + - * / ( )
    数字常量            100.0 / 2.5
    metric:名称[@版本]   引用指标（@版本 用于跨版本引用）
    dim:名称            引用维度
    sum(...) avg(...) count(...)   包裹函数

归一化（canonical form）：
    1) 去掉所有空白；
    2) 丢掉括号节点，只在需要时按运算优先级重新输出括号；
    3) 去掉 *1 / 1* / /1 / +0 / -0 这类恒等常量；
    4) 数字常量化（100.0 -> 100，2.50 -> 2.5）；
    5) 统一 `metric:` / `dim:` 前缀与函数名小写，去掉冒号两侧空格。

归一化只做结构层面的等价变换：`a/b` 与 `a/b*100` 归一化后仍然不同，
这正是口径冲突检测要保留的语义差异。
"""

from __future__ import annotations

import re

MAX_LENGTH = 2000
MAX_DEPTH = 60
FUNCTIONS = ("sum", "avg", "count")

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_REF_RE = re.compile(r"(?:metric|dim)\s*:\s*[A-Za-z_][A-Za-z0-9_]*(?:\s*@\s*\d+)?", re.IGNORECASE)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REF_PARTS_RE = re.compile(
    r"^(metric|dim)\s*:\s*([A-Za-z_][A-Za-z0-9_]*)(?:\s*@\s*(\d+))?$", re.IGNORECASE
)


class ExprError(ValueError):
    """表达式非法：带位置信息，便于页面定位。"""

    def __init__(self, message: str, position: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.position = position

    def as_dict(self) -> dict:
        return {"error": self.message, "position": self.position}


# --------------------------------------------------------------------------- 词法


def tokenize(text: str) -> list[tuple[str, str, int]]:
    tokens: list[tuple[str, str, int]] = []
    pos = 0
    size = len(text)
    while pos < size:
        ch = text[pos]
        if ch.isspace():
            pos += 1
            continue
        match = _NUM_RE.match(text, pos)
        if match:
            tokens.append(("num", match.group(0), pos))
            pos = match.end()
            continue
        match = _REF_RE.match(text, pos)
        if match:
            tokens.append(("ref", match.group(0), pos))
            pos = match.end()
            continue
        match = _IDENT_RE.match(text, pos)
        if match:
            tokens.append(("ident", match.group(0), pos))
            pos = match.end()
            continue
        if ch in "+-*/()":
            tokens.append(("op", ch, pos))
            pos += 1
            continue
        raise ExprError(f"无法识别的字符 {ch!r}", pos)
    tokens.append(("eof", "", size))
    return tokens


def _parse_ref(text: str, pos: int) -> tuple:
    match = _REF_PARTS_RE.match(text.strip())
    if not match:
        raise ExprError(f"引用格式错误：{text!r}", pos)
    kind = match.group(1).lower()
    name = match.group(2)
    version = match.group(3)
    if kind == "dim" and version is not None:
        raise ExprError("dim 维度引用不支持 @版本", pos)
    return ("ref", kind, name, int(version) if version is not None else None)


# --------------------------------------------------------------------------- 语法
# AST 节点（元组，便于比较与序列化）：
#   ("num", float)
#   ("ref", "metric"|"dim", name, version|None)
#   ("call", "sum"|"avg"|"count", node)
#   ("bin", "+"|"-"|"*"|"/", left, right)
#   ("neg", node)


class _Parser:
    def __init__(self, tokens: list[tuple[str, str, int]]) -> None:
        self._tokens = tokens
        self._i = 0
        self._depth = 0

    def peek(self, offset: int = 0) -> tuple[str, str, int]:
        index = min(self._i + offset, len(self._tokens) - 1)
        return self._tokens[index]

    def take(self) -> tuple[str, str, int]:
        token = self._tokens[self._i]
        self._i += 1
        return token

    def parse(self) -> tuple:
        self._enter(self.peek()[2])
        node = self._expr()
        self._leave()
        kind, value, pos = self.peek()
        if kind != "eof":
            raise ExprError(f"多余的内容 {value!r}", pos)
        return node

    def _enter(self, pos: int) -> None:
        self._depth += 1
        if self._depth > MAX_DEPTH:
            raise ExprError(f"表达式嵌套超过 {MAX_DEPTH} 层", pos)

    def _leave(self) -> None:
        self._depth -= 1

    def _expect_close(self) -> None:
        kind, value, pos = self.peek()
        if kind != "op" or value != ")":
            raise ExprError("缺少右括号", pos)
        self.take()

    def _expr(self) -> tuple:
        node = self._term()
        while True:
            kind, value, _pos = self.peek()
            if kind == "op" and value in "+-":
                self.take()
                node = ("bin", value, node, self._term())
            else:
                return node

    def _term(self) -> tuple:
        node = self._unary()
        while True:
            kind, value, _pos = self.peek()
            if kind == "op" and value in "*/":
                self.take()
                node = ("bin", value, node, self._unary())
            else:
                return node

    def _unary(self) -> tuple:
        kind, value, pos = self.peek()
        if kind == "op" and value in "+-":
            self.take()
            child = self._unary()
            if value == "+":
                return child
            return ("neg", child)
        return self._primary()

    def _primary(self) -> tuple:
        kind, value, pos = self.peek()
        if kind == "num":
            self.take()
            return ("num", float(value))
        if kind == "ref":
            self.take()
            return _parse_ref(value, pos)
        if kind == "ident":
            self.take()
            name = value.lower()
            if name not in FUNCTIONS:
                raise ExprError(f"未知函数 {value!r}（只支持 sum/avg/count）", pos)
            kw_kind, kw_value, kw_pos = self.peek()
            if kw_kind != "op" or kw_value != "(":
                raise ExprError(f"{name} 后面缺少括号", pos)
            self.take()
            if self.peek()[1] == ")":
                raise ExprError(f"{name}() 需要一个参数", kw_pos)
            arg = self._expr()
            self._expect_close()
            return ("call", name, arg)
        if kind == "op" and value == "(":
            self.take()
            inner = self._expr()
            self._expect_close()
            return inner
        raise ExprError(f"此处需要数字、metric:/dim: 引用或括号，实际是 {value!r}", pos)


# --------------------------------------------------------------------------- 归一化

_PRECEDENCE = {"+": 1, "-": 1, "*": 2, "/": 2}


def _format_number(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value - round(value)) < 1e-12 and abs(value) < 1e15:
        return str(int(round(value)))
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return text


def _is_number(node: tuple, value: float) -> bool:
    return node[0] == "num" and abs(node[1] - value) < 1e-12


def simplify(node: tuple) -> tuple:
    """恒等常量消去：*1、1*、/1、+0、-0，以及负数折叠。"""
    kind = node[0]
    if kind == "bin":
        op = node[1]
        left = simplify(node[2])
        right = simplify(node[3])
        if op == "*":
            if _is_number(right, 1.0):
                return left
            if _is_number(left, 1.0):
                return right
        if op == "/" and _is_number(right, 1.0):
            return left
        if op == "+":
            if _is_number(right, 0.0):
                return left
            if _is_number(left, 0.0):
                return right
        if op == "-" and _is_number(right, 0.0):
            return left
        return ("bin", op, left, right)
    if kind == "call":
        return ("call", node[1], simplify(node[2]))
    if kind == "neg":
        inner = simplify(node[1])
        if inner[0] == "num":
            return ("num", -inner[1])
        if inner[0] == "neg":
            return inner[1]
        return ("neg", inner)
    return node


def _render(node: tuple) -> str:
    kind = node[0]
    if kind == "num":
        return _format_number(node[1])
    if kind == "ref":
        suffix = f"@{node[3]}" if node[3] is not None else ""
        return f"{node[1]}:{node[2]}{suffix}"
    if kind == "call":
        return f"{node[1]}({_render(node[2])})"
    if kind == "neg":
        inner = node[1]
        text = _render(inner)
        if inner[0] in ("bin", "neg"):
            return f"-({text})"
        return f"-{text}"
    op, left, right = node[1], node[2], node[3]
    return f"{_wrap(left, op, 'left')}{op}{_wrap(right, op, 'right')}"


def _wrap(child: tuple, parent_op: str, side: str) -> str:
    text = _render(child)
    if child[0] == "neg":
        return f"({text})"
    if child[0] != "bin":
        return text
    parent_prec = _PRECEDENCE[parent_op]
    child_prec = _PRECEDENCE[child[1]]
    if child_prec < parent_prec:
        return f"({text})"
    if child_prec == parent_prec and side == "right":
        # a-(b+c) / a/(b*c)：结合性不同必须保留括号
        return f"({text})"
    return text


# --------------------------------------------------------------------------- 对外接口


def parse_expression(text: str | None) -> tuple:
    if text is None or not str(text).strip():
        raise ExprError("表达式为空", 0)
    raw = str(text).strip()
    if len(raw) > MAX_LENGTH:
        raise ExprError(f"表达式超过 {MAX_LENGTH} 字符", 0)
    return _Parser(tokenize(raw)).parse()


def normalize_expression(text: str | None) -> str:
    return _render(simplify(parse_expression(text)))


def collect_references(node: tuple) -> list[dict]:
    """按出现顺序收集引用（去重，保留首次出现的版本）。"""
    found: list[dict] = []
    seen: set[tuple] = set()

    def walk(current: tuple) -> None:
        kind = current[0]
        if kind == "ref":
            key = (current[1], current[2], current[3])
            if key not in seen:
                seen.add(key)
                found.append({"kind": current[1], "name": current[2], "version": current[3]})
            return
        if kind in ("bin",):
            walk(current[2])
            walk(current[3])
            return
        if kind in ("call", "neg"):
            walk(current[2] if kind == "call" else current[1])

    walk(node)
    return found


def collect_functions(node: tuple) -> list[str]:
    found: list[str] = []

    def walk(current: tuple) -> None:
        kind = current[0]
        if kind == "call":
            if current[1] not in found:
                found.append(current[1])
            walk(current[2])
        elif kind == "bin":
            walk(current[2])
            walk(current[3])
        elif kind == "neg":
            walk(current[1])

    walk(node)
    return found


def analyze_expression(text: str | None) -> dict:
    """页面与 API 共用的一份解析结果。"""
    try:
        node = parse_expression(text)
    except ExprError as exc:
        return {
            "ok": False,
            "raw": (text or "").strip(),
            "normalized": None,
            "error": exc.message,
            "position": exc.position,
            "references": [],
            "metrics": [],
            "dims": [],
            "functions": [],
        }
    normalized = _render(simplify(node))
    references = collect_references(node)
    return {
        "ok": True,
        "raw": str(text).strip(),
        "normalized": normalized,
        "error": None,
        "position": None,
        "references": references,
        "metrics": [ref for ref in references if ref["kind"] == "metric"],
        "dims": [ref for ref in references if ref["kind"] == "dim"],
        "functions": collect_functions(node),
    }


def same_caliber(left: str, right: str) -> bool:
    """两个表达式是否同一口径（归一化后完全一致）。"""
    try:
        return normalize_expression(left) == normalize_expression(right)
    except ExprError:
        return False
