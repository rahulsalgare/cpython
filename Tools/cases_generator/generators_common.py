from pathlib import Path

from analyzer import (
    Instruction,
    Properties,
    StackItem,
    analysis_error,
    Label,
    CodeSection,
)
from cwriter import CWriter
from typing import Callable, TextIO, Iterator, Iterable
from lexer import Token
from stack import Storage, StackError

# Set this to true for voluminous output showing state of stack and locals
PRINT_STACKS = False

class TokenIterator:

    look_ahead: Token | None
    iterator: Iterator[Token]

    def __init__(self, tkns: Iterable[Token]):
        self.iterator = iter(tkns)
        self.look_ahead = None

    def __iter__(self) -> "TokenIterator":
        return self

    def __next__(self) -> Token:
        if self.look_ahead is None:
            return next(self.iterator)
        else:
            res = self.look_ahead
            self.look_ahead = None
            return res

    def peek(self) -> Token | None:
        if self.look_ahead is None:
            for tkn in self.iterator:
                self.look_ahead = tkn
                break
        return self.look_ahead

ROOT = Path(__file__).parent.parent.parent.resolve()
DEFAULT_INPUT = (ROOT / "Python/bytecodes.c").as_posix()


def root_relative_path(filename: str) -> str:
    try:
        return Path(filename).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        # Not relative to root, just return original path.
        return filename


def type_and_null(var: StackItem) -> tuple[str, str]:
    if var.type:
        return var.type, "NULL"
    elif var.is_array():
        return "_PyStackRef *", "NULL"
    else:
        return "_PyStackRef", "PyStackRef_NULL"


def write_header(
    generator: str, sources: list[str], outfile: TextIO, comment: str = "//"
) -> None:
    outfile.write(
        f"""{comment} This file is generated by {root_relative_path(generator)}
{comment} from:
{comment}   {", ".join(root_relative_path(src) for src in sources)}
{comment} Do not edit!
"""
    )


def emit_to(out: CWriter, tkn_iter: TokenIterator, end: str) -> Token:
    parens = 0
    for tkn in tkn_iter:
        if tkn.kind == end and parens == 0:
            return tkn
        if tkn.kind == "LPAREN":
            parens += 1
        if tkn.kind == "RPAREN":
            parens -= 1
        out.emit(tkn)
    raise analysis_error(f"Expecting {end}. Reached end of file", tkn)


ReplacementFunctionType = Callable[
    [Token, TokenIterator, CodeSection, Storage, Instruction | None], bool
]

def always_true(tkn: Token | None) -> bool:
    if tkn is None:
        return False
    return tkn.text in {"true", "1"}

NON_ESCAPING_DEALLOCS = {
    "_PyFloat_ExactDealloc",
    "_PyLong_ExactDealloc",
    "_PyUnicode_ExactDealloc",
}

class Emitter:
    out: CWriter
    labels: dict[str, Label]
    _replacers: dict[str, ReplacementFunctionType]

    def __init__(self, out: CWriter, labels: dict[str, Label]):
        self._replacers = {
            "EXIT_IF": self.exit_if,
            "DEOPT_IF": self.deopt_if,
            "ERROR_IF": self.error_if,
            "ERROR_NO_POP": self.error_no_pop,
            "DECREF_INPUTS": self.decref_inputs,
            "DEAD": self.kill,
            "INPUTS_DEAD": self.kill_inputs,
            "SYNC_SP": self.sync_sp,
            "SAVE_STACK": self.save_stack,
            "RELOAD_STACK": self.reload_stack,
            "PyStackRef_CLOSE_SPECIALIZED": self.stackref_close_specialized,
            "PyStackRef_AsPyObjectSteal": self.stackref_steal,
            "DISPATCH": self.dispatch,
            "INSTRUCTION_SIZE": self.instruction_size,
            "stack_pointer": self.stack_pointer,
        }
        self.out = out
        self.labels = labels

    def dispatch(
        self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:
        if storage.spilled:
            raise analysis_error("stack_pointer needs reloading before dispatch", tkn)
        self.emit(tkn)
        return False

    def deopt_if(
        self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:
        self.out.start_line()
        self.out.emit("if (")
        lparen = next(tkn_iter)
        assert lparen.kind == "LPAREN"
        first_tkn = tkn_iter.peek()
        emit_to(self.out, tkn_iter, "RPAREN")
        self.emit(") {\n")
        next(tkn_iter)  # Semi colon
        assert inst is not None
        assert inst.family is not None
        family_name = inst.family.name
        self.emit(f"UPDATE_MISS_STATS({family_name});\n")
        self.emit(f"assert(_PyOpcode_Deopt[opcode] == ({family_name}));\n")
        self.emit(f"JUMP_TO_PREDICTED({family_name});\n")
        self.emit("}\n")
        return not always_true(first_tkn)

    exit_if = deopt_if

    def goto_error(self, offset: int, label: str, storage: Storage) -> str:
        if offset > 0:
            return f"JUMP_TO_LABEL(pop_{offset}_{label});"
        if offset < 0:
            storage.copy().flush(self.out)
        return f"JUMP_TO_LABEL({label});"

    def error_if(
        self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:
        lparen = next(tkn_iter)
        assert lparen.kind == "LPAREN"
        first_tkn = tkn_iter.peek()
        unconditional = always_true(first_tkn)
        if unconditional:
            next(tkn_iter)
            comma = next(tkn_iter)
            if comma.kind != "COMMA":
                raise analysis_error(f"Expected comma, got '{comma.text}'", comma)
            self.out.start_line()
        else:
            self.out.emit_at("if ", tkn)
            self.emit(lparen)
            emit_to(self.out, tkn_iter, "COMMA")
            self.out.emit(") {\n")
        label = next(tkn_iter).text
        next(tkn_iter)  # RPAREN
        next(tkn_iter)  # Semi colon
        storage.clear_inputs("at ERROR_IF")

        c_offset = storage.stack.peek_offset()
        try:
            offset = -int(c_offset)
        except ValueError:
            offset = -1
        self.out.emit(self.goto_error(offset, label, storage))
        self.out.emit("\n")
        if not unconditional:
            self.out.emit("}\n")
        return not unconditional

    def error_no_pop(
        self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:
        next(tkn_iter)  # LPAREN
        next(tkn_iter)  # RPAREN
        next(tkn_iter)  # Semi colon
        self.out.emit_at(self.goto_error(0, "error", storage), tkn)
        return False

    def decref_inputs(
        self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:
        next(tkn_iter)
        next(tkn_iter)
        next(tkn_iter)
        try:
            storage.close_inputs(self.out)
        except StackError as ex:
            raise analysis_error(ex.args[0], tkn)
        except Exception as ex:
            ex.args = (ex.args[0] + str(tkn),)
            raise
        return True

    def kill_inputs(
        self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:
        next(tkn_iter)
        next(tkn_iter)
        next(tkn_iter)
        for var in storage.inputs:
            var.defined = False
        return True

    def kill(
        self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:
        next(tkn_iter)
        name_tkn = next(tkn_iter)
        name = name_tkn.text
        next(tkn_iter)
        next(tkn_iter)
        for var in storage.inputs:
            if var.name == name:
                var.defined = False
                break
        else:
            raise analysis_error(
                f"'{name}' is not a live input-only variable", name_tkn
            )
        return True

    def stackref_kill(
        self,
        name: Token,
        storage: Storage,
        escapes: bool
    ) -> bool:
        live = ""
        for var in reversed(storage.inputs):
            if var.name == name.text:
                if live and escapes:
                    raise analysis_error(
                        f"Cannot close '{name.text}' when "
                        f"'{live}' is still live", name)
                var.defined = False
                break
            if var.defined:
                live = var.name
        return True

    def stackref_close_specialized(
        self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:

        self.out.emit(tkn)
        tkn = next(tkn_iter)
        assert tkn.kind == "LPAREN"
        self.out.emit(tkn)
        name = next(tkn_iter)
        self.out.emit(name)
        comma = next(tkn_iter)
        if comma.kind != "COMMA":
            raise analysis_error("Expected comma", comma)
        self.out.emit(comma)
        dealloc = next(tkn_iter)
        if dealloc.kind != "IDENTIFIER":
            raise analysis_error("Expected identifier", dealloc)
        self.out.emit(dealloc)
        if name.kind == "IDENTIFIER":
            escapes = dealloc.text not in NON_ESCAPING_DEALLOCS
            return self.stackref_kill(name, storage, escapes)
        rparen = emit_to(self.out, tkn_iter, "RPAREN")
        self.emit(rparen)
        return True

    def stackref_steal(
        self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:
        self.out.emit(tkn)
        tkn = next(tkn_iter)
        assert tkn.kind == "LPAREN"
        self.out.emit(tkn)
        name = next(tkn_iter)
        self.out.emit(name)
        if name.kind == "IDENTIFIER":
            return self.stackref_kill(name, storage, False)
        rparen = emit_to(self.out, tkn_iter, "RPAREN")
        self.emit(rparen)
        return True

    def sync_sp(
        self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:
        next(tkn_iter)
        next(tkn_iter)
        next(tkn_iter)
        storage.clear_inputs("when syncing stack")
        storage.flush(self.out)
        self._print_storage(storage)
        return True

    def stack_pointer(
        self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:
        if storage.spilled:
            raise analysis_error("stack_pointer is invalid when stack is spilled to memory", tkn)
        self.emit(tkn)
        return True

    def goto_label(self, goto: Token, label: Token, storage: Storage) -> None:
        if label.text not in self.labels:
            print(self.labels.keys())
            raise analysis_error(f"Label '{label.text}' does not exist", label)
        label_node = self.labels[label.text]
        if label_node.spilled:
            if not storage.spilled:
                self.emit_save(storage)
        elif storage.spilled:
            raise analysis_error("Cannot jump from spilled label without reloading the stack pointer", goto)
        self.out.start_line()
        self.out.emit("JUMP_TO_LABEL(")
        self.out.emit(label)
        self.out.emit(")")

    def emit_save(self, storage: Storage) -> None:
        storage.save(self.out)
        self._print_storage(storage)

    def save_stack(
        self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:
        next(tkn_iter)
        next(tkn_iter)
        next(tkn_iter)
        self.emit_save(storage)
        return True

    def emit_reload(self, storage: Storage) -> None:
        storage.reload(self.out)
        self._print_storage(storage)

    def reload_stack(
        self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:
        next(tkn_iter)
        next(tkn_iter)
        next(tkn_iter)
        self.emit_reload(storage)
        return True

    def instruction_size(self,
        tkn: Token,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> bool:
        """Replace the INSTRUCTION_SIZE macro with the size of the current instruction."""
        if uop.instruction_size is None:
            raise analysis_error("The INSTRUCTION_SIZE macro requires uop.instruction_size to be set", tkn)
        self.out.emit(f" {uop.instruction_size} ")
        return True

    def _print_storage(self, storage: Storage) -> None:
        if PRINT_STACKS:
            self.out.start_line()
            self.emit(storage.as_comment())
            self.out.start_line()

    def _emit_if(
        self,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> tuple[bool, Token, Storage]:
        """Returns (reachable?, closing '}', stack)."""
        tkn = next(tkn_iter)
        assert tkn.kind == "LPAREN"
        self.out.emit(tkn)
        rparen = emit_to(self.out, tkn_iter, "RPAREN")
        self.emit(rparen)
        if_storage = storage.copy()
        reachable, rbrace, if_storage = self._emit_block(tkn_iter, uop, if_storage, inst, True)
        try:
            maybe_else = tkn_iter.peek()
            if maybe_else and maybe_else.kind == "ELSE":
                self._print_storage(storage)
                self.emit(rbrace)
                self.emit(next(tkn_iter))
                maybe_if = tkn_iter.peek()
                if maybe_if and maybe_if.kind == "IF":
                    # Emit extra braces around the if to get scoping right
                    self.emit(" {\n")
                    self.emit(next(tkn_iter))
                    else_reachable, rbrace, else_storage = self._emit_if(tkn_iter, uop, storage, inst)
                    self.out.start_line()
                    self.emit("}\n")
                else:
                    else_reachable, rbrace, else_storage = self._emit_block(tkn_iter, uop, storage, inst, True)
                if not reachable:
                    # Discard the if storage
                    reachable = else_reachable
                    storage = else_storage
                elif not else_reachable:
                    # Discard the else storage
                    storage = if_storage
                    reachable = True
                else:
                    if PRINT_STACKS:
                        self.emit("/* Merge */\n")
                    else_storage.merge(if_storage, self.out)
                    storage = else_storage
                    self._print_storage(storage)
            else:
                if reachable:
                    if PRINT_STACKS:
                        self.emit("/* Merge */\n")
                    if_storage.merge(storage, self.out)
                    storage = if_storage
                    self._print_storage(storage)
                else:
                    # Discard the if storage
                    reachable = True
        except StackError as ex:
            self._print_storage(if_storage)
            raise analysis_error(ex.args[0], rbrace) # from None
        return reachable, rbrace, storage

    def _emit_block(
        self,
        tkn_iter: TokenIterator,
        uop: CodeSection,
        storage: Storage,
        inst: Instruction | None,
        emit_first_brace: bool
    ) -> tuple[bool, Token, Storage]:
        """ Returns (reachable?, closing '}', stack)."""
        braces = 1
        out_stores = set(uop.output_stores)
        tkn = next(tkn_iter)
        reload: Token | None = None
        try:
            reachable = True
            line : int = -1
            if tkn.kind != "LBRACE":
                raise analysis_error(f"PEP 7: expected '{{', found: {tkn.text}", tkn)
            escaping_calls = uop.properties.escaping_calls
            if emit_first_brace:
                self.emit(tkn)
            self._print_storage(storage)
            for tkn in tkn_iter:
                if PRINT_STACKS and tkn.line != line:
                    self.out.start_line()
                    self.emit(storage.as_comment())
                    self.out.start_line()
                    line = tkn.line
                if tkn in escaping_calls:
                    escape = escaping_calls[tkn]
                    if escape.kills is not None:
                        if tkn == reload:
                            self.emit_reload(storage)
                        self.stackref_kill(escape.kills, storage, True)
                        self.emit_save(storage)
                    elif tkn != reload:
                        self.emit_save(storage)
                    reload = escape.end
                elif tkn == reload:
                    self.emit_reload(storage)
                if tkn.kind == "LBRACE":
                    self.out.emit(tkn)
                    braces += 1
                elif tkn.kind == "RBRACE":
                    self._print_storage(storage)
                    braces -= 1
                    if braces == 0:
                        return reachable, tkn, storage
                    self.out.emit(tkn)
                elif tkn.kind == "GOTO":
                    label_tkn = next(tkn_iter)
                    self.goto_label(tkn, label_tkn, storage)
                    reachable = False
                elif tkn.kind == "IDENTIFIER":
                    if tkn.text in self._replacers:
                        if not self._replacers[tkn.text](tkn, tkn_iter, uop, storage, inst):
                            reachable = False
                    else:
                        if tkn in out_stores:
                            for out in storage.outputs:
                                if out.name == tkn.text:
                                    out.defined = True
                                    out.in_memory = False
                                    break
                        if tkn.text.startswith("DISPATCH"):
                            self._print_storage(storage)
                            reachable = False
                        self.out.emit(tkn)
                elif tkn.kind == "IF":
                    self.out.emit(tkn)
                    if_reachable, rbrace, storage = self._emit_if(tkn_iter, uop, storage, inst)
                    if reachable:
                        reachable = if_reachable
                    self.out.emit(rbrace)
                else:
                    self.out.emit(tkn)
        except StackError as ex:
            raise analysis_error(ex.args[0], tkn) from None
        raise analysis_error("Expecting closing brace. Reached end of file", tkn)

    def emit_tokens(
        self,
        code: CodeSection,
        storage: Storage,
        inst: Instruction | None,
    ) -> Storage:
        tkn_iter = TokenIterator(code.body)
        self.out.start_line()
        reachable, rbrace, storage = self._emit_block(tkn_iter, code, storage, inst, False)
        try:
            if reachable:
                self._print_storage(storage)
                storage.push_outputs()
                self._print_storage(storage)
        except StackError as ex:
            raise analysis_error(ex.args[0], rbrace) from None
        return storage

    def emit(self, txt: str | Token) -> None:
        self.out.emit(txt)


def cflags(p: Properties) -> str:
    flags: list[str] = []
    if p.oparg:
        flags.append("HAS_ARG_FLAG")
    if p.uses_co_consts:
        flags.append("HAS_CONST_FLAG")
    if p.uses_co_names:
        flags.append("HAS_NAME_FLAG")
    if p.jumps:
        flags.append("HAS_JUMP_FLAG")
    if p.has_free:
        flags.append("HAS_FREE_FLAG")
    if p.uses_locals:
        flags.append("HAS_LOCAL_FLAG")
    if p.eval_breaker:
        flags.append("HAS_EVAL_BREAK_FLAG")
    if p.deopts:
        flags.append("HAS_DEOPT_FLAG")
    if p.side_exit:
        flags.append("HAS_EXIT_FLAG")
    if not p.infallible:
        flags.append("HAS_ERROR_FLAG")
    if p.error_without_pop:
        flags.append("HAS_ERROR_NO_POP_FLAG")
    if p.escapes:
        flags.append("HAS_ESCAPES_FLAG")
    if p.pure:
        flags.append("HAS_PURE_FLAG")
    if p.no_save_ip:
        flags.append("HAS_NO_SAVE_IP_FLAG")
    if p.oparg_and_1:
        flags.append("HAS_OPARG_AND_1_FLAG")
    if flags:
        return " | ".join(flags)
    else:
        return "0"
