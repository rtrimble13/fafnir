"""pandas and numpy, loaded the first time duk uses them rather than at start.

duk is run as a process per question -- portable alone runs one per ticker and
one per held stretch of it -- and every process used to pay about 0.3s to load
pandas and numpy, `duk th` and `duk tc` included, which use neither.

The modules the CLI loads at start take them from here::

    from duk._deferred import pandas as pd

and not with ``import pandas as pd``: an ``import`` statement makes the import
system read the module's ``__spec__``, which loads a deferred module on the
spot, so a plain import anywhere on the start-up path undoes the deferral.
Reading the name from this module does not. ``test_cli_startup.py`` asserts
what importing the CLI does and does not load.

Both are registered in ``sys.modules`` (:func:`duk._lazy.lazy_import`), so any
later ``import pandas``, here or in another library, gets this same module --
loaded by then -- and never a second copy.
"""

from __future__ import annotations

from duk._lazy import lazy_import

pandas = lazy_import("pandas")
numpy = lazy_import("numpy")
