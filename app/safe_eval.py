"""受限 Python 表达式求值器：AST 白名单 + 自研解释器，全程零 eval。

两道防线：
1. `_check`：静态白名单审查。仅允许表达式级语法（字面量、变量、算术/比较/布尔、
   条件表达式、列表/元组/字典/集合及推导式、下标与切片、lambda、白名单内置函数调用）；
   禁止属性访问、下划线开头名称、import、任何语句、任意函数调用；
   另有资源护栏（幂指数必须是 ≤1000 的常量、数字字面量 ≤1e12、表达式长度）。
2. `_eval_node`：手写 AST 解释器直接对白名单语法树求值——**不经过 eval/exec/compile**，
   即使白名单出现疏漏，也不存在把表达式交还给 Python 求值器的瞬间。

开放实现说明：解释器按节点类型逐个求值，语义对齐 Python 原生行为
（短路布尔、链式比较、推导式独立作用域、生成器惰性求值、字典解包等）。
"""
import ast
import operator

ALLOWED_NODES = {
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Store,  # 推导式目标变量（如 [x for x in rows]）的上下文标记，仅作模式识别
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Lambda,
    ast.arg,
    ast.arguments,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.Subscript,
    ast.Slice,
    ast.Starred,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.Call,
    ast.keyword,
    # 运算符
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not,
    ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Is, ast.IsNot,
}

MAX_POWER = 1000  # 防止 9**9**9 之类的资源耗尽
MAX_RANGE_LEN = 1_000_000  # 防 sum(range(10**12)) 这类合法表达式长期占住执行线程


def _safe_range(*args):
    """range 的受限包装：迭代量超过上限直接拒绝，而不是让执行线程空转数小时。"""
    r = range(*args)
    if len(r) > MAX_RANGE_LEN:
        raise ValueError(f"range 迭代量过大（>{MAX_RANGE_LEN}），已拒绝执行")
    return r


ALLOWED_FUNCS = {
    "len": len, "sum": sum, "min": min, "max": max, "round": round,
    "abs": abs, "sorted": sorted, "reversed": reversed, "list": list,
    "dict": dict, "set": set, "tuple": tuple, "str": str, "int": int,
    "float": float, "bool": bool, "range": _safe_range, "enumerate": enumerate,
    "zip": zip, "any": any, "all": all,
}

_BIN_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Not: operator.not_}
_CMP_OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
    ast.In: lambda a, b: operator.contains(b, a),
    ast.NotIn: lambda a, b: not operator.contains(b, a),
}


class UnsafeExpressionError(ValueError):
    """表达式包含白名单之外的语法或危险名称。"""


def _check(node: ast.AST) -> None:
    if type(node) not in ALLOWED_NODES:
        raise UnsafeExpressionError(f"禁止的语法: {type(node).__name__}")

    if isinstance(node, ast.Name) and (
        node.id.startswith("__") or node.id.startswith("_")
    ):
        raise UnsafeExpressionError(f"禁止访问名称: {node.id}")

    if isinstance(node, ast.arg) and node.arg.startswith("_"):
        raise UnsafeExpressionError(f"禁止的参数名: {node.arg}")

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        # 指数必须是小的常量，防止超大数计算
        exp = node.right
        if not (isinstance(exp, ast.Constant) and isinstance(exp.value, (int, float))
                and abs(exp.value) <= MAX_POWER):
            raise UnsafeExpressionError("幂运算仅允许绝对值不超过1000的常量指数")

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool) and abs(node.value) > 10**12:
        raise UnsafeExpressionError("数字字面量过大")

    for child in ast.iter_child_nodes(node):
        _check(child)


def check_expression(expression: str):
    """静态安全审查（不求值）：通过则返回白名单审查过的 AST 树，供解释器求值。

    供工作流在**保存定义时**提前拒绝危险表达式，与 safe_eval 共用同一套白名单规则。
    """
    if not isinstance(expression, str) or not expression.strip():
        raise UnsafeExpressionError("表达式为空")
    if len(expression) > 20000:
        raise UnsafeExpressionError("表达式过长")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpressionError(f"表达式语法错误: {exc}") from exc
    _check(tree)
    return tree


def safe_eval(expression: str, variables: dict | None = None) -> object:
    """在白名单与解释器约束下求值一个 Python 表达式。

    variables: 允许在表达式中引用的变量名 -> 值。
    """
    tree = check_expression(expression)
    env: dict = dict(ALLOWED_FUNCS)
    env.update(variables or {})
    try:
        return _eval_node(tree.body, env)
    except UnsafeExpressionError:
        raise
    except Exception as exc:
        raise ValueError(f"表达式执行出错: {type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------- 解释器
def _eval_node(node, env):
    """对白名单内的 AST 节点求值。默认分支拒绝——纵深防御的第二道闸。"""
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id not in env:
            raise NameError(f"name '{node.id}' is not defined")
        return env[node.id]

    if isinstance(node, ast.BinOp):
        fn = _BIN_OPS.get(type(node.op))
        if fn is None:
            raise UnsafeExpressionError(f"禁止的运算符: {type(node.op).__name__}")
        return fn(_eval_node(node.left, env), _eval_node(node.right, env))

    if isinstance(node, ast.UnaryOp):
        fn = _UNARY_OPS.get(type(node.op))
        if fn is None:
            raise UnsafeExpressionError(f"禁止的运算符: {type(node.op).__name__}")
        return fn(_eval_node(node.operand, env))

    if isinstance(node, ast.BoolOp):
        # 短路求值：and 返回第一个假值，or 返回第一个真值，均无则返回最后一个
        is_and = isinstance(node.op, ast.And)
        result = None
        for child in node.values:
            result = _eval_node(child, env)
            if is_and and not result:
                return result
            if not is_and and result:
                return result
        return result

    if isinstance(node, ast.Compare):
        # 链式比较（a < b < c）：等价 a < b and b < c，同样短路
        left = _eval_node(node.left, env)
        for op, comparator in zip(node.ops, node.comparators):
            fn = _CMP_OPS.get(type(op))
            if fn is None:
                raise UnsafeExpressionError(f"禁止的比较符: {type(op).__name__}")
            right = _eval_node(comparator, env)
            if not fn(left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.IfExp):
        branch = node.body if _eval_node(node.test, env) else node.orelse
        return _eval_node(branch, env)

    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        items = []
        for elt in node.elts:
            if isinstance(elt, ast.Starred):
                items.extend(_eval_node(elt.value, env))
            else:
                items.append(_eval_node(elt, env))
        if isinstance(node, ast.Tuple):
            return tuple(items)
        if isinstance(node, ast.Set):
            return set(items)
        return items

    if isinstance(node, ast.Dict):
        # {**a, "k": v}：key 为 None 的项是字典解包
        out: dict = {}
        for key_node, value_node in zip(node.keys, node.values):
            if key_node is None:
                out.update(_eval_node(value_node, env))
            else:
                out[_eval_node(key_node, env)] = _eval_node(value_node, env)
        return out

    if isinstance(node, ast.Subscript):
        value = _eval_node(node.value, env)
        return value[_eval_node(node.slice, env)]

    if isinstance(node, ast.Slice):
        lower = _eval_node(node.lower, env) if node.lower else None
        upper = _eval_node(node.upper, env) if node.upper else None
        step = _eval_node(node.step, env) if node.step else None
        return slice(lower, upper, step)

    if isinstance(node, ast.Lambda):
        return _make_lambda(node, env)

    if isinstance(node, ast.Call):
        fn = _eval_node(node.func, env)
        if not callable(fn):
            raise TypeError(f"'{type(fn).__name__}' object is not callable")
        args = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                args.extend(_eval_node(arg.value, env))
            else:
                args.append(_eval_node(arg, env))
        kwargs = {}
        for kw in node.keywords:
            if kw.arg is None:          # **unpack
                kwargs.update(_eval_node(kw.value, env))
            else:
                kwargs[kw.arg] = _eval_node(kw.value, env)
        return fn(*args, **kwargs)

    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        def _gen():
            yield from _run_comprehension(node.generators, node.elt, None, dict(env))
        if isinstance(node, ast.GeneratorExp):
            return _gen()
        items = list(_gen())
        return set(items) if isinstance(node, ast.SetComp) else items

    if isinstance(node, ast.DictComp):
        items = list(_run_comprehension(node.generators, None, (node.key, node.value), dict(env)))
        return dict(items)

    # Store 上下文只出现在推导式目标里，由 _assign 处理；到这里即未知语法
    raise UnsafeExpressionError(f"禁止的语法: {type(node).__name__}")


def _make_lambda(node: ast.Lambda, env):
    """把 Lambda 节点包装成真正的 Python 可调用对象（sorted(key=lambda ...) 需要真实函数）。"""
    def fn(*fargs, **fkwargs):
        local = dict(env)
        _bind_args(node.args, fargs, fkwargs, local)
        return _eval_node(node.body, local)
    return fn


def _bind_args(arguments: ast.arguments, fargs: tuple, fkwargs: dict, local: dict):
    """把调用方实参绑定到参数表（位置参数、默认值、*vararg、**kwarg）。"""
    pos_params = [a.arg for a in (arguments.posonlyargs + arguments.args)]
    defaults = [None] * (len(pos_params) - len(arguments.defaults)) + arguments.defaults
    for i, name in enumerate(pos_params):
        if i < len(fargs):
            local[name] = fargs[i]
        elif name in fkwargs:
            local[name] = fkwargs.pop(name)
        elif defaults[i] is not None:
            local[name] = _eval_node(defaults[i], local)
        else:
            raise TypeError(f"missing required argument: '{name}'")
    if len(fargs) > len(pos_params):
        if arguments.vararg is None:
            raise TypeError("too many positional arguments")
        local[arguments.vararg.arg] = tuple(fargs[len(pos_params):])
    extra_kw = {k: v for k, v in fkwargs.items()
                if all(k != a.arg for a in arguments.kwonlyargs)}
    if extra_kw and arguments.kwarg is None:
        raise TypeError(f"unexpected keyword argument: '{next(iter(extra_kw))}'")
    for a, d in zip(arguments.kwonlyargs, arguments.kw_defaults):
        if a.arg in fkwargs:
            local[a.arg] = fkwargs[a.arg]
            extra_kw.pop(a.arg, None)
        elif d is not None:
            local[a.arg] = _eval_node(d, local)
        else:
            raise TypeError(f"missing required keyword argument: '{a.arg}'")
    if arguments.kwarg is not None:
        local[arguments.kwarg.arg] = extra_kw


def _assign(target, value, env):
    """推导式目标绑定：支持单个名字与元组/列表解包（[k for k, v in rows]）。"""
    if isinstance(target, ast.Name):
        env[target.id] = value
    elif isinstance(target, (ast.Tuple, ast.List)):
        values = list(value)
        if len(values) != len(target.elts):
            raise ValueError(f"cannot unpack: expected {len(target.elts)} values, got {len(values)}")
        for t, v in zip(target.elts, values):
            _assign(t, v, env)
    else:
        raise UnsafeExpressionError(f"禁止的推导式目标: {type(target).__name__}")


def _run_comprehension(generators, elt, kv, env):
    """按 comprehension 的生成器序列迭代（支持多级 for 与 if 过滤）。

    语义对齐 Python：第一个生成器的可迭代对象在**外层**作用域求值，
    之后的生成器在推导式作用域内求值；推导式自带独立作用域，迭代变量不外泄。
    """
    def _level(index, current_env):
        if index == len(generators):
            if kv is None:
                yield _eval_node(elt, current_env)
            else:
                yield (_eval_node(kv[0], current_env), _eval_node(kv[1], current_env))
            return
        comp = generators[index]
        iterable = _eval_node(comp.iter, current_env if index == 0 else env)
        for item in iterable:
            child = dict(current_env)
            _assign(comp.target, item, child)
            if all(_eval_node(if_, child) for if_ in comp.ifs):
                yield from _level(index + 1, child)

    yield from _level(0, dict(env))
