import triton
import triton.language as tl
import ast
import inspect
import copy
from functools import reduce
from contextlib import contextmanager

# ---------- AST Helpers -------------

def replace_ast_name(root, name, new_node):
    class Visitor(ast.NodeTransformer):
        def __init__(self, old_name, new_node):
            self.old_name = old_name
            self.new_node = new_node
        
        def visit_Name(self, node):
            return self.new_node if node.id==self.old_name else node

    return Visitor(name, new_node).visit(root)

# ------------------------------------
# ---------- Meta Classes ------------

class meta_tuple:

    def __init__(self, *names):
        self.names = names
    
    def expand_args(self):
        return [ast.arg(arg=name) for name in self.names]


class meta_macro:
    
    def __init__(self, fn) -> None:
        self.fn = fn
    
    def __str__(self):
        return self.fn.__name__

    def expand_args(self):
        return []

    def __call__(self, scope, node, lengths):
        func_name_node = ast.Name(id=str(self.fn.__name__), ctx=ast.Load())
        ret = ast.Call(func=func_name_node, args=node.args, keywords=node.keywords)
        return ret
        
# ------------------------------------
# ---------- Meta Iterators ----------
    
class IndexIterator:

    def __init__(self, name: ast.Name, length: int):
        assert isinstance(name, ast.Name)
        self.name = name
        self.length = length
        self.index = 0
    
    def __iter__(self):
        return self

    def __next__(self):
        if self.index >= self.length:
            raise StopIteration
        index_node = ast.Index(value=ast.Constant(self.index))
        self.index += 1  
        return ast.Subscript(value=self.name, slice=index_node, ctx=self.name.ctx)


def _make_iterator(node, lengths):
    if isinstance(node, ast.Name):
        return IndexIterator(node, lengths[node.id])
    if isinstance(node, ast.Call):
        name = node.func.id
        if name == 'meta_zip':
            return zip(*[_make_iterator(arg, lengths) for arg in node.args])
        assert False
    assert False

def meta_zip(*args):
    pass

# ------------------------------------
# ---------- Meta Functions ----------

def meta_map(scope, node, lengths):
    fn_args = node.args[1].args.args
    fn_body = node.args[1].body
    elts = [replace_ast_name(fn_body, fn_args[0].arg, new_node) for new_node in _make_iterator(node.args[0], lengths)]
    return ast.Tuple(elts=elts, ctx=ast.Load())

def meta_for_each(scope, node, lengths):
    idx = next((i for i, v in enumerate(scope.body) if v == node or (isinstance(v, ast.Expr) and v.value==node)))
    scope.body.pop(idx)
    for i, iter in enumerate(_make_iterator(node.args[0], lengths)):
        body = copy.deepcopy(node.args[1].body)
        args = copy.deepcopy(node.args[1].args.args)
        for old_arg, new_arg in zip(args, iter):
            replace_ast_name(body, old_arg.arg, new_arg)
        scope.body.insert(idx+i, ast.copy_location(ast.Expr(body), node))
    return scope.body[idx]


# ------------------------------------
# ----- Template Specialization ------

    
class SpecializationVisitor(ast.NodeTransformer):
    def __init__(self, symbols):
        self.symbols = symbols
        self.whitelist = {}
        self.whitelist = {meta_zip, meta_map, meta_for_each}
        self.whitelist |= {v for v in symbols.values() if v.__class__ in [meta_tuple, meta_macro]}
        self.scopes = []
        self.lengths = dict()
        super().__init__()
    
    @contextmanager
    def scope(self, node):
        try:
            self.scopes.append(node)
            yield
        finally:
            self.scopes.pop()
            
    def visit_For(self, node):
        with self.scope(node):
            self.generic_visit(node)
        return node
    
    def visit_While(self, node):
        with self.scope(node):
            self.generic_visit(node)
        return node
    
    def visit_If(self, node):
        with self.scope(node):
            self.generic_visit(node)
        return node
    
    def _resolve_symbol(self, node):
        if isinstance(node, ast.Name) and self.symbols.get(node.id, None) in self.whitelist:
            return self.symbols[node.id]
        if isinstance(node, str) and self.symbols.get(node, None) in self.whitelist:
            return self.symbols[node]
        return None

    def visit_Tuple(self, node):
        self.lengths[node] = len(node.elts)
        return self.generic_visit(node)

    def visit_FunctionDef(self, node):
        with self.scope(node):
            for arg in node.args.args:
                sym = self.symbols.get(arg.arg)
                if isinstance(sym, meta_tuple):
                    elts = [ast.Name(id=name) for name in sym.names]
                    value = ast.Tuple(elts=elts, ctx=ast.Load())
                    targets = [ast.Name(id=arg.arg, ctx=ast.Store())]
                    node.body.insert(0,  ast.Assign(targets=targets, value=value, lineno=None))
            symbols = {arg: self._resolve_symbol(arg.arg) for arg in node.args.args}
            args = [[k] if v is None else v.expand_args() for k, v in symbols.items()]
            node.args.args = reduce(list.__add__, args)
            self.generic_visit(node)
        return node
    
    def visit_Assign(self, node):
        value = self.visit(node.value)
        value_length = self.lengths.get(value)
        if len(node.targets) == 1 and value_length is not None:
            self.lengths[node.targets[0].id] = value_length
        return self.generic_visit(node)
        
    def visit_Call(self, node):
        # print(ast.unparse(node))
        if (impl := self._resolve_symbol(node.func)) is None:
            return self.generic_visit(node)
        ret = ast.copy_location(impl(self.scopes[-1], node, self.lengths), node)
        return self.visit(ret)


def specialize(func, **kwargs):
    source = inspect.getsource(func)
    tree = ast.parse(source)
    symbols = globals()
    symbols.update({k: v for k,v in kwargs.items() if isinstance(v, meta_tuple)})
    symbols.update({k: meta_macro(v) for k,v in kwargs.items() if callable(v)})
    visitor = SpecializationVisitor(symbols=symbols)
    new_tree = visitor.visit(tree)
    new_source = ast.unparse(new_tree)

    return new_source


# ------------------------------------
# ------------- Template -------------

def normalize(Outs: meta_tuple, Ins: meta_tuple, 
              stride_om, stride_im, M, N, BLOCK_N: tl.constexpr,
              InitState: meta_macro,
              UpdateState: meta_macro,
              ApplyState: meta_macro):
    pid_m = tl.program_id(0)
    pid_n = tl.program_id(1)
    Ins = meta_map(Ins, lambda In: In + pid_m*stride_im)
    Outs = meta_map(Outs, lambda Out: Out + pid_m*stride_om)
    curr_state = InitState()
    for start_n in range(pid_n*BLOCK_N, N, BLOCK_N):
        offs_n = start_n + tl.arange(0, BLOCK_N)
        mask_n = offs_n < N
        inputs = meta_map(Ins, lambda In: tl.load(In + offs_n, mask=mask_n))
        curr_state = UpdateState(curr_state, inputs)
    final_state = curr_state
    for start_n in range(pid_n*BLOCK_N, N, BLOCK_N):
        offs_n = start_n + tl.arange(0, BLOCK_N)
        inputs = meta_map(Ins, lambda In: tl.load(In + offs_n, mask=mask_n))
        outputs = meta_map(inputs, lambda x: ApplyState(final_state, x))
        meta_for_each(meta_zip(Outs, outputs), 
                         lambda Out, out: tl.store(Out + offs_n, out, mask=mask_n))

@triton.jit
def softmax_init():
    return 0., float("-inf")

@triton.jit
def softmax_update(state, value):
    m_ip1 = tl.maximum(state[1], tl.max(value, axis=0))
    d_ip1 = state[0] * tl.exp(state[1] - m_ip1) + tl.sum(tl.exp(value - m_ip1), 0)
    return d_ip1, m_ip1

@triton.jit
def softmax_apply(state, value):
    return tl.exp(value - state[1]) / state[0]


softmax = specialize(normalize, 
                    Ins=meta_tuple('X'), 
                    Outs=meta_tuple('Y'),
                    InitState=softmax_init, 
                    UpdateState=softmax_update,
                    ApplyState=softmax_apply)
print(softmax)