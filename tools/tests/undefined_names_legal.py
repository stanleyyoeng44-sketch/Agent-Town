"""Legal Python that MUST NOT be reported. Each case is a way a naive
undefined-name checker reports a false positive."""
import os
import sys
from os.path import join as _join
from collections import OrderedDict  # noqa
from math import *  # noqa: F401,F403  -- star import at MODULE level is legal

MODULE_LATE = None


def uses_later_module_constant():
    return LATE_CONSTANT          # legal: resolved at call time


LATE_CONSTANT = 1                  # bound after the function that uses it


class K:
    attr = 1

    def m(self):
        return K.attr + self.attr   # class name and self.attr


def closures_and_defaults(a, b=2, *args, c=3, **kw):
    d = a + b + c
    e = args, kw
    def inner():
        return d + e            # closure over enclosing locals
    return inner


def comprehension_scoping(xs):
    return [x * y for x in xs for y in xs if x > 0]


def comp_uses_outer(xs):
    limit = 3
    return [x for x in xs if x < limit]      # `limit` from enclosing scope


def nested_comp(matrix):
    return [[c for c in row] for row in matrix]


def lam():
    f = lambda q, r=2: q + r
    return f(1)


def exception_name():
    try:
        int("x")
    except ValueError as err:
        return str(err)


def iteration_targets():
    total = 0
    for i in range(3):
        total += i
    for k, v in {"a": 1}.items():
        total += v
    with open(os.devnull) as fh:
        pass
    return total


def walrus(seq):
    if (n := len(seq)) > 0:
        return n
    return 0


def annotated(a: int, b: "str") -> bool:
    c: float = 1.0
    return bool(a) and bool(b) and c > 0


def star_import_case():
    # The star import is at MODULE level (line 7), which is the only legal
    # place for it. `sqrt` comes from there. A checker that ignores star
    # imports reports a false positive here -- and `from math import *`
    # inside a function is a COMPILE error, not a parse error, so it must
    # not appear in this file: this file has to stay importable.
    return sqrt(4)


def globals_case():
    name = "MODULE_LATE"
    return globals()[name]


def del_and_rebind():
    x = 1
    del x
    x = 2
    return x


def multiple_targets():
    a = b = c = 0
    a, b = b, a
    return a + b + c


async def async_fn():
    import asyncio
    await asyncio.sleep(0)
    return [i async for i in _agen()]


async def _agen():
    yield 1


def class_scope_classvars():
    class Inner:
        VALUE = 3

        @staticmethod
        def get():
            return Inner.VALUE
    return Inner.get()


def global_stmt():
    global MODULE_LATE
    MODULE_LATE = 5


def main():
    return (uses_later_module_constant(), K().m(), closures_and_defaults(1),
            comprehension_scoping([1]), comp_uses_outer([1]), nested_comp([[1]]),
            lam(), exception_name(), iteration_targets(), walrus([1]),
            annotated(1, "a"), star_import_case(), globals_case(),
            del_and_rebind(), multiple_targets(), class_scope_classvars())


if __name__ == "__main__":
    main()
