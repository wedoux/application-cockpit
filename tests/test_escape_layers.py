"""
Escape closes the topmost layer, not the whole stack.
======================================================
The report modals sit at z-index 51 and the role drawer at 41, so a modal
opened over the drawer must close first and leave the drawer where it was.
Getting the order backwards is silently destructive: you press Escape to
dismiss a scan report you glanced at, and lose the role you had open
underneath.

These are assertions about the template's source rather than about a live
DOM, and it's worth being clear about what that does and doesn't buy. It
pins the two things that actually regress — a second Escape handler being
added somewhere else in the file, and the drawer drifting up the layer
list — but it cannot prove the handler behaves correctly in a browser.
Proving that needs a real DOM, and putting Playwright plus a Chrome binary
in a suite that otherwise runs on a bare clone would cost every contributor
more than this one behaviour is worth.
"""
import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).parent.parent / "templates" / "index.html"


@pytest.fixture(scope="module")
def source():
    return TEMPLATE.read_text(encoding="utf-8")


def test_exactly_one_escape_handler(source):
    """Two handlers would each fire on one keypress, closing two layers —
    exactly the collapse this ordering exists to prevent."""
    handlers = re.findall(r'e\.key\s*[!=]==\s*"Escape"', source)
    assert len(handlers) == 1, f"expected one Escape handler, found {len(handlers)}"


def test_both_report_modals_are_dismissable(source):
    layers = source[source.index("const escapeLayers"):source.index("document.addEventListener(\"keydown\"")]
    assert "closeStagedModal" in layers
    assert "closeScanModal" in layers


def test_the_drawer_is_the_last_layer(source):
    """Topmost-first ordering: whatever else is open, the drawer is only
    reached once nothing is stacked on top of it."""
    layers = source[source.index("const escapeLayers"):source.index("document.addEventListener(\"keydown\"")]
    positions = {name: layers.index(name) for name in
                 ("closeStagedModal", "closeScanModal", "closeDrawer")}
    assert positions["closeDrawer"] == max(positions.values())


def test_only_the_topmost_layer_is_closed(source):
    """The handler must close one layer per keypress. A loop, or a series of
    independent ifs, would close every open layer at once."""
    handler = source[source.index('document.addEventListener("keydown"'):]
    handler = handler[:handler.index("});") + 3]
    assert ".find(" in handler, "the handler should select a single layer, not iterate over all of them"
    assert ".forEach" not in handler and ".filter(" not in handler
