"""seed_menu_config：research section/item 存在且不破坏幂等契约。"""
import importlib.util, pathlib


def _loadSeedModule():
    path = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "seed_menu_config.py"
    spec = importlib.util.spec_from_file_location("seed_menu_config", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_research_section_and_items_present() -> None:
    mod = _loadSeedModule()
    sectionCodes = {s["code"] for s in mod.SECTIONS}
    assert "section.research" in sectionCodes
    itemPaths = {i["path"] for i in mod.ITEMS}
    assert "/research" in itemPaths and "/research/compare" in itemPaths


def test_sort_order_spacing() -> None:
    mod = _loadSeedModule()
    research = [i for i in mod.ITEMS if str(i["path"]).startswith("/research")]
    assert all(i["sort_order"] % 10 == 0 for i in research)
