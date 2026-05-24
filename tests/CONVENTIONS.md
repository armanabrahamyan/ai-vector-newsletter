# Test Conventions — AI Vector

*Owner: Test Engineer. Module engineers read this before writing tests.*

The whole bar is one question:

> **"If I delete the line of code this test covers, does this test fail?"**

If yes, the test earns its keep.
If no, it's documentation pretending to be a test. Don't add it.

---

## 1. Test behaviour, not implementation

**Bad** — pinned to *how* something works:
```python
def test_summarise_calls_llm_twice():
    with mock_llm() as m:
        summarise(issue)
    assert m.call_count == 2
```

**Good** — pinned to *what* it produces:
```python
def test_summarise_returns_one_summaryblock_per_top_story(ranked):
    issue = summarise(ranked)
    assert len(issue.pulse.stories) == 1
    assert all(s.headline for s in issue.pulse.stories)
```

The first test breaks every refactor. The second test only breaks when
the contract changes.

---

## 2. Don't test the framework

Pydantic already tests that `Literal["a", "b"]` rejects `"c"`. The
typing system already enforces return types. If your test is asserting
something the framework guarantees, delete it.

**Don't write:**
```python
def test_section_name_literal_rejects_unknown():
    with pytest.raises(ValidationError):
        IssueSection(name="not_a_section", stories=[])  # Pydantic tests this
```

**Do write:**
```python
def test_pulse_section_must_contain_exactly_one_story():
    """Editorial invariant — pulse is THE story of the day, not zero, not two."""
    with pytest.raises(ValidationError, match="exactly 1 story"):
        IssueSection(name="pulse", stories=[])
```

The second test exercises an invariant *we* added (in the model_validator).
The first test would still pass even if we removed our code.

---

## 3. Don't test mocks

If your test mocks something to return `X` and then asserts the unit
returns `X`, you've tested the mock.

**Bad:**
```python
def test_fetch_returns_items():
    with mock.patch("src.fetch._get") as m:
        m.return_value = [Item(...)]
        assert fetch() == [Item(...)]  # Just testing the mock
```

**Good:**
```python
def test_fetch_normalises_html_in_summary():
    """The unit under test is the *normalisation*, not the HTTP call."""
    raw_feed = make_fixture_feed(summary="<p>hello <b>world</b></p>")
    with mock.patch("src.fetch._get", return_value=raw_feed):
        items = fetch()
    assert "<p>" not in items[0].raw_summary
    assert "hello world" in items[0].raw_summary
```

Mock the boundary; assert on the work the unit actually does.

---

## 4. Pin behaviour at the seam, not at the leaf

A test on `parse_feed()` that pins exact output for a fixture feed is
worth ten tests on the internal helpers it calls. Seam tests survive
refactors; leaf tests break with every cleanup.

Prefer one rich integration-style unit test (parse this XML → get these
Items) over ten micro-tests on private helpers.

---

## 5. One failure mode per test

When a test breaks, the reader should know exactly which invariant is
gone. Compound assertions hide which invariant failed.

**Bad:**
```python
def test_cluster_is_valid(c):
    assert c.size == 2
    assert c.cluster_id.startswith("c_")
    assert c.canonical_title
    assert len(c.sources) == 2
```

**Good:**
```python
class TestCluster:
    def test_size_matches_item_count(self, c): assert c.size == 2
    def test_id_pattern_matches(self, c): assert re.match(r"^c_[0-9a-f]{12,}$", c.cluster_id)
    def test_canonical_title_is_set(self, c): assert c.canonical_title
    def test_sources_deduplicated(self, c): assert sorted(c.sources) == ["a", "b"]
```

Parametrize when the *invariant* is one thing and the *cases* are many.

---

## 6. Property-based tests where they pay off

Use Hypothesis for things like:

- `RankedStory.score = sum(weight * sub_score)` over the full integer
  space.
- Cluster idempotency: re-clustering an already-clustered set gives the
  same result.
- Atomic-write safety: any partial-write at any byte offset leaves the
  canonical file untouched.

Don't reach for Hypothesis for everything. Hand-crafted cases are easier
to read and debug when the input space is small.

---

## 7. Regression tests are mandatory

Every bug that escapes to ratification gets a test added to `tests/`
**before** the fix ships. The fix PR shows the test going from red to
green. No exceptions.

This is how the suite stays grounded. It's also the only way the
"performative tests" problem stays solved — every test in the suite has
a story for why it exists.

---

## 8. Fixtures: small, synthetic, frozen-in-time

- Use the fixtures in `conftest.py` where they fit. Add module-specific
  fixtures inline in your test file when they don't.
- **Never** use `datetime.now()` in tests. Use `FIXED_NOW`,
  `FIXED_EARLIER`, `FIXED_DATE` from `conftest`.
- Fixture data should be obviously synthetic: cluster IDs like
  `c_aaaaaaaaaaaa`, URLs like `https://example.com/...`, source names
  like `example_blog`. Real-looking data confuses readers.
- Filesystem writes go to the `tmp_data_root` fixture, never to the real
  `data/` tree.

---

## 9. Test layout

Group with classes; don't run them as functions in a flat file:

```python
class TestFooBehaviour:
    def test_normal_case(self): ...
    def test_edge_case(self): ...

class TestFooErrors:
    def test_raises_on_invalid_input(self): ...
```

Classes give the test runner a hierarchical readout and let related
tests share setup naturally.

---

## 10. What to mock, what not to mock

| Don't mock | Do mock |
|---|---|
| Pydantic models | The LLM (`_llm_call`) |
| `paths` helpers | The embedding model encoder |
| Standard library (json, datetime, pathlib) | `httpx` / `feedparser` for fetch |
| Jinja2 template rendering | The actual filesystem (use `tmp_path`) |
| The unit under test | External APIs (always) |

If you're tempted to mock the standard library, you're probably testing
the wrong thing.

---

## Examples in this repo

Look at `tests/test_models.py::TestRankedStory::test_score_must_equal_weighted_breakdown`
for the platonic ideal: tests a load-bearing invariant the LLM cannot be
trusted on, fails loud if removed, uses synthetic data, one assertion
per logical case.

Compare to (hypothetical) `test_score_is_an_int` — that's the framework's
job, not yours. Pydantic guarantees the type.

---

## When to push back

If a module engineer's PR adds a test that violates these conventions,
the right response is a review comment quoting the relevant section and
proposing the load-bearing alternative. Not a flat "no" — a "here's the
shape that earns its keep."

Keep the bar high, keep the tone collegial.
