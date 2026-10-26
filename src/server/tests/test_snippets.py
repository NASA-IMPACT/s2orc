import pytest

from snippets import (
    ELLIPSIS,
    Match,
    best_cluster,
    best_snippet,
    content_end,
    content_start,
    extract_passages,
    find_matches,
    make_stemmer,
    query_stems,
    words_after,
    words_before,
)


@pytest.fixture
def stemmer():
    return make_stemmer()


def _words(s: str):
    return s.strip(ELLIPSIS).split()


def test_query_stems_drops_stopwords_and_stems(stemmer):
    stems = query_stems("The relationship between branched-chain amino acids and insulin resistance", stemmer)
    assert "the" not in stems and "and" not in stems
    assert {"branch", "chain", "amino", "acid", "insulin", "resist"} <= stems


def test_stem_matching_hits_inflected_forms(stemmer):
    stems = query_stems("resistance studies", stemmer)
    text = "Many resistant strains were examined in this study of bacteria."
    words = {text[m.start:m.end] for m in find_matches(text, stems, stemmer)}
    assert words == {"resistant", "study"}


def test_candidate_prefilter_is_word_anchored_and_case_insensitive(stemmer):
    stems = query_stems("chain", stemmer)
    text = "Chains of blockchain and CHAIN-link; unchained."
    words = [text[m.start:m.end] for m in find_matches(text, stems, stemmer)]
    assert words == ["Chains", "CHAIN"]  # not "blockchain", not "unchained"


def test_words_before_and_after():
    text = "w0 w1 w2 w3 w4 w5 w6 w7 w8 w9"
    pos = text.index("w5")
    assert text[words_before(text, pos, 2):pos] == "w3 w4 "
    assert text[words_before(text, pos, 50):pos] == "w0 w1 w2 w3 w4 "  # clamps at start
    end = pos + 2
    assert text[end:words_after(text, end, 2)] == " w6 w7"
    assert text[end:words_after(text, end, 50)] == " w6 w7 w8 w9"  # clamps at end


def test_context_words_on_both_sides(stemmer):
    before = " ".join(f"before{i}" for i in range(200))
    after = " ".join(f"after{i}" for i in range(200))
    text = f"{before} amino acid chain {after}"
    snip = best_snippet(text, query_stems("amino acid chain", stemmer), stemmer, context_words=5)
    assert snip.startswith(ELLIPSIS) and snip.endswith(ELLIPSIS)
    assert _words(snip) == [f"before{i}" for i in range(195, 200)] + ["amino", "acid", "chain"] + [f"after{i}" for i in range(5)]


def test_context_is_word_based_regardless_of_match_spread(stemmer):
    # Matches spread over a long paragraph still get full context on each side.
    middle = "insulin " + " ".join(f"m{i}" for i in range(60)) + " resistance"
    text = "L9 L8 L7 L6 L5 L4 L3 L2 L1 " + middle + " R1 R2 R3 R4 R5 R6 R7 R8 R9"
    snip = best_snippet(text, query_stems("insulin resistance", stemmer), stemmer, context_words=3)
    w = _words(snip)
    assert w[:3] == ["L3", "L2", "L1"] and w[-3:] == ["R1", "R2", "R3"]
    assert "insulin" in w and "resistance" in w


def test_densest_cluster_first_and_other_matches_listed(stemmer):
    filler = " ".join(f"w{i}" for i in range(150))
    text = (
        f"insulin was mentioned once here. {filler} "
        f"Later, insulin resistance and amino acid levels were measured together. {filler} "
        f"Finally amino acids appear again."
    )
    stems = query_stems("insulin resistance amino acid", stemmer)
    passages, total = extract_passages(text, stems, stemmer, context_words=3, max_passages=5)
    assert total == 7
    # (distinct + density) / (4 + 1): 4 terms once -> 4.5/5, 2 terms -> 2.5/5, 1 term -> 1.5/5.
    assert [p.score for p in passages] == [0.9, 0.5, 0.3]
    assert "resistance" in passages[0].content and "amino acid" in passages[0].content
    assert "appear again" in passages[1].content
    assert "mentioned once" in passages[2].content
    # Passages never overlap.
    spans = sorted((p.start, p.end) for p in passages)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))


def test_max_passages_caps_the_list(stemmer):
    text = " ".join(f"insulin x{i} " + " ".join(f"f{i}_{k}" for k in range(80)) for i in range(10))
    passages, total = extract_passages(text, query_stems("insulin", stemmer), stemmer, context_words=2, max_passages=3)
    assert total == 10 and len(passages) == 3


def test_clamps_at_text_start_and_end(stemmer):
    text = "Insulin resistance is the topic. " + " ".join(f"z{i}" for i in range(100))
    snip = best_snippet(text, query_stems("insulin resistance", stemmer), stemmer, context_words=4)
    assert not snip.startswith(ELLIPSIS) and snip.endswith(ELLIPSIS)
    assert snip.startswith("Insulin resistance")

    text2 = " ".join(f"z{i}" for i in range(100)) + " ends with insulin resistance."
    snip2 = best_snippet(text2, query_stems("insulin resistance", stemmer), stemmer, context_words=4)
    assert snip2.startswith(ELLIPSIS) and not snip2.endswith(ELLIPSIS)
    assert snip2.endswith("insulin resistance.")


def test_snaps_to_word_boundaries_and_collapses_whitespace(stemmer):
    text = "alpha\n\nbeta   gamma delta epsilon zeta eta theta iota kappa lambda mu nu"
    snip = best_snippet(text, query_stems("epsilon", stemmer), stemmer, context_words=2)
    inner = snip.strip(ELLIPSIS)
    assert "\n" not in inner and "  " not in inner
    assert inner == "gamma delta epsilon zeta eta"


def test_highlight_wraps_matches(stemmer):
    text = "Branched-chain amino acids were elevated in resistant subjects."
    snip = best_snippet(text, query_stems("amino resistance", stemmer), stemmer, context_words=20, highlight=True)
    assert "<b>amino</b>" in snip and "<b>resistant</b>" in snip
    assert "<b>Branched</b>" not in snip


def test_fallback_when_no_match(stemmer):
    text = "This paper is about something else entirely. " * 20
    passages, total = extract_passages(text, query_stems("quantum chromodynamics", stemmer), stemmer, context_words=4)
    assert total == 0 and len(passages) == 1
    assert passages[0].score == 0.0
    assert passages[0].content.startswith("This paper is about")
    assert len(_words(passages[0].content)) == 12  # 3 * context_words


def test_start_at_skips_front_matter_but_falls_back(stemmer):
    front = "Jane Doe Department of Insulin Studies University of Somewhere\n\n"
    body = "Abstract. We measured amino acid levels in forty patients."
    text = front + body
    snip = best_snippet(text, query_stems("amino acid", stemmer), stemmer, context_words=20, start_at=len(front))
    assert "Jane Doe" not in snip and "amino acid" in snip
    snip2 = best_snippet(text, query_stems("insulin", stemmer), stemmer, context_words=20, start_at=len(front))
    assert "Insulin" in snip2


def test_end_at_prefers_body_over_reference_list(stemmer):
    body = "In this cohort insulin resistance correlated with amino acid levels. " + " ".join(f"b{i}" for i in range(60))
    refs = " ".join("Insulin resistance and amino acids. J Amino Acid Res." for _ in range(5))
    text = body + "\n\nReferences\n" + refs
    stems = query_stems("insulin resistance amino acid", stemmer)
    snip = best_snippet(text, stems, stemmer, context_words=5, end_at=len(body))
    assert "cohort" in snip and "J Amino Acid Res" not in snip
    snip2 = best_snippet(text, query_stems("Res", stemmer), stemmer, context_words=5, end_at=len(body))
    assert "Res" in snip2


def test_passage_score_coverage_dominates_density():
    from snippets import passage_score

    once = [Match(i * 10, i * 10 + 5, s) for i, s in enumerate("abcdef")]
    assert passage_score(once, 6) == round(6.5 / 7, 3)
    thrice = once * 3
    assert passage_score(thrice, 6) == round((6 + 0.75) / 7, 3)
    five_heavy = [m for m in once if m.stem != "f"] * 10
    assert passage_score(five_heavy, 6) < passage_score(once, 6)
    assert passage_score([], 6) == 0.0


def test_best_cluster_prefers_more_distinct_stems():
    matches = [Match(0, 5, "a"), Match(6, 11, "a"), Match(12, 17, "a"), Match(300, 305, "a"), Match(306, 311, "b")]
    assert best_cluster(matches, span_chars=50) == (3, 4)


def test_content_start_reads_json_string_annotations():
    ann = {"abstract": None, "paragraph": '[{"end":4772,"start":956},{"end":5970,"start":4784}]'}
    assert content_start(ann) == 956
    assert content_start({"abstract": '[{"start": 500, "end": 900}]', "paragraph": '[{"start": 956, "end": 1}]'}) == 500
    assert content_start(None) == 0
    assert content_start({"paragraph": "not json"}) == 0
    assert content_start({"paragraph": [{"start": "15", "end": 86}]}) == 15


def test_content_end():
    ann = {"bibentry": '[{"attributes":{"id":"b0"},"end":20857,"start":20535},{"end":21000,"start":20900}]'}
    assert content_end(ann, 50000) == 20535
    assert content_end(ann, 10000) == 10000
    assert content_end({}, 500) == 500
    assert content_end(None, 500) == 500
