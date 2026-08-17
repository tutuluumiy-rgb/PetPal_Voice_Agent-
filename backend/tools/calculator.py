"""计算器工具：ast 白名单安全求值（禁用 eval/exec）

支持：数字、四则运算、括号、幂、常用数学函数（sqrt/abs/round/min/max 等）
拒绝：属性访问、导入、函数调用以外的任何语句（防 __import__、os.system 等注入）
"""

import ast
import math
import operator as _op

# 允许的二元运算符映射
_BIN_OPS = {
    ast.Add: _op.add,
    ast.Sub: _op.sub,
    ast.Mult: _op.mul,
    ast.Div: _op.truediv,
    ast.FloorDiv: _op.floordiv,
    ast.Mod: _op.mod,
    ast.Pow: _op.pow,
}

# 允许的一元运算符映射
_UNARY_OPS = {
    ast.UAdd: _op.pos,
    ast.USub: _op.neg,
}

# 允许的函数白名单
_FUNCS = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "pow": pow,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
}


def _eval_node(node):
    """递归安全求值 AST 节点，遇到不支持的节点抛 ValueError"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"不支持的字面量: {type(node.value).__name__}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        if name not in _FUNCS:
            raise ValueError(f"不允许的函数: {name}")
        args = [_eval_node(a) for a in node.args]
        if node.keywords:
            raise ValueError("不支持关键字参数")
        return _FUNCS[name](*args)
    if isinstance(node, ast.Name) and node.id in ("pi", "e"):
        return math.pi if node.id == "pi" else math.e
    raise ValueError(f"不支持的表达式: {type(node).__name__}")


async def calculator(expression: str) -> str:
    """安全计算数学表达式，返回结果字符串。

    参数:
        expression: 数学表达式，如 "(3+5)*2"、"sqrt(144)+10"
    """
    if not expression or not expression.strip():
        return "错误：表达式为空"
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        result = _eval_node(tree)
        # 浮点数结果保留 4 位小数
        if isinstance(result, float):
            result = round(result, 4)
        return f"计算结果：{expression} = {result}"
    except Exception as e:
        return f"错误：无法计算「{expression}」（{e}）"
