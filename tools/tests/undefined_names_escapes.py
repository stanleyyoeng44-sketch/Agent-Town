"""Files that MUST make the checker stay SILENT for a whole module.

The undefined-name checker returns "no findings" for a whole file when it sees
one of these, because each one can make an arbitrary name resolve at runtime
and a checker cannot prove otherwise. The set below is EXACTLY
undefined_names.ESCAPE_CALLS plus the star-import and NameError cases -- note
__import__ is NOT in it: the tool does not treat __import__() as an escape,
so a file using it IS checked. (An earlier version of this docstring listed
__import__; that was wrong and is the doc-outruns-predicate shape.)

  * a star import (`from x import *`)
  * a call to globals()/locals()/eval()/exec()/vars()
  * an `except NameError:` handler (a deliberate probe)

Those conservatisms are CORRECT. This fixture measures them, so the control
is not merely "the legal file is clean" (which a silent-for-the-wrong-reason
checker also passes). C12 runs this file and requires exit 0; the file
itself contains names that a non-silencing checker WOULD report, so if the
short-circuit ever breaks, this control goes red.

WHY IT IS SEPARATE FROM undefined_names_legal.py: that file is the
FALSE-POSITIVE control and must not carry a module-wide escape hatch, or the
checker short-circuits it and it proves nothing. Keeping the two fixtures
apart is what lets each one fail for its own reason.

WHY THE THREE CASES BELOW LIVE IN THIS ONE FILE AND ARE *ALSO* REPEATED
PER-HATCH INSIDE C12 (agent-2 found this, life 18): stacking all three
hatches in a single file makes the control OVER-DETERMINED -- any one of them
keeps the file silent, so deleting one from the tool changes nothing and this
fixture still passes. agent-2 measured exactly that: removing the tool's
`except NameError` short-circuit left this control and all 56 selftest arms
green. This file stays as an end-to-end check that a realistic file with
hatches is silent; the per-hatch arms in c12_undefined_names(), each paired
with a hatch-removed complement, are what make each conservatism individually
load-bearing.
"""
from math import *  # noqa: F401,F403  -- the escape hatch under test


def uses_undefined_via_star_import():
    # `from math import *` makes any math name potentially defined; the
    # checker cannot see sqrt here, so it must stay silent for the module
    # rather than report a false positive.
    return sqrt(16)


def uses_undefined_via_globals():
    # globals() can resolve any name the checker never saw bound.
    name = "MODULE_LATE"
    return globals()[name]


def catches_name_error_as_a_probe():
    # A deliberate NameError probe means the module is EXPECTED to contain
    # undefined names; the checker stays quiet rather than second-guess it.
    try:
        return also_never_defined_anywhere      # noqa: F821
    except NameError:
        return None
