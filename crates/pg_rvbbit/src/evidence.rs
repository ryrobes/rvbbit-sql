//! `rvbbit.text_evidence(long_text, query, top_n)` — sentence-level
//! relevance highlighting, no index required.
//!
//! Use case: pair with semantic predicates / knn to show users WHY a
//! row matched, or to trim context before sending to an LLM.
//!
//!   SELECT body, rvbbit.text_evidence(body, 'angry refund') AS why
//!   FROM tickets
//!   WHERE rvbbit.means(body, 'angry customer')
//!   LIMIT 10;
//!
//! Algorithm: split into sentences, score each by query-term coverage
//! (count of distinct query terms appearing) plus a small bonus for
//! repeated terms. Returns the top-N sentences in original order.
//! Deterministic, no allocation surprises, ~µs per call.
//!
//! For richer scoring (BM25 + IDF + stemming) wire in Tantivy via the
//! sidecar in RYR-293; this UDF is the lightweight inline variant.

use pgrx::prelude::*;

/// Top-N relevant sentences from `text` for `query`. `top_n` defaults
/// to 3. Returns sentences in original order (not score order) so the
/// flow of the text is preserved.
#[pg_extern(immutable, parallel_safe)]
fn text_evidence(text: &str, query: &str, top_n: default!(i32, 3)) -> Vec<String> {
    if text.is_empty() || query.is_empty() || top_n <= 0 {
        return Vec::new();
    }
    let n = top_n as usize;
    let sentences = split_sentences(text);
    if sentences.is_empty() {
        return Vec::new();
    }
    let q_terms: Vec<String> = tokenize(query);
    if q_terms.is_empty() {
        return Vec::new();
    }

    // Score each sentence: distinct-term-coverage * 10 + total-term-count.
    let scores: Vec<(usize, i32)> = sentences
        .iter()
        .enumerate()
        .map(|(i, s)| (i, score_sentence(s, &q_terms)))
        .collect();

    // Take top-N by score (stable: ties keep original order).
    let mut sorted: Vec<(usize, i32)> = scores.into_iter().filter(|(_, s)| *s > 0).collect();
    sorted.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
    sorted.truncate(n);

    // Restore original order so the output reads naturally.
    let mut indices: Vec<usize> = sorted.into_iter().map(|(i, _)| i).collect();
    indices.sort_unstable();
    indices.into_iter().map(|i| sentences[i].clone()).collect()
}

/// Lower-cased, punctuation-stripped tokens. Latin alphanumerics only
/// (good enough for the inline-evidence use case; for multilingual go
/// through Tantivy + charabia).
fn tokenize(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut buf = String::new();
    for c in s.chars() {
        if c.is_alphanumeric() {
            buf.push(c.to_ascii_lowercase());
        } else if !buf.is_empty() {
            if buf.len() >= 2 {
                out.push(std::mem::take(&mut buf));
            } else {
                buf.clear();
            }
        }
    }
    if buf.len() >= 2 {
        out.push(buf);
    }
    out
}

/// Split on .!? followed by whitespace, plus newline-as-separator.
/// Returns trimmed, non-empty sentences in document order.
fn split_sentences(text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut prev_terminal = false;
    for c in text.chars() {
        if prev_terminal && c.is_whitespace() {
            let t = cur.trim().to_string();
            if !t.is_empty() {
                out.push(t);
            }
            cur.clear();
            prev_terminal = false;
            continue;
        }
        cur.push(c);
        prev_terminal = matches!(c, '.' | '!' | '?' | '\n');
    }
    let t = cur.trim().to_string();
    if !t.is_empty() {
        out.push(t);
    }
    out
}

fn score_sentence(s: &str, q_terms: &[String]) -> i32 {
    let toks = tokenize(s);
    if toks.is_empty() {
        return 0;
    }
    use std::collections::HashSet;
    let q_set: HashSet<&String> = q_terms.iter().collect();
    let mut hits = 0i32;
    let mut distinct = HashSet::new();
    for t in &toks {
        if q_set.contains(t) {
            hits += 1;
            distinct.insert(t.clone());
        }
    }
    distinct.len() as i32 * 10 + hits
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_relevant_sentence() {
        let text = "The weather is nice today. \
                    Angry customer wants a refund immediately. \
                    Bye for now.";
        let evidence = text_evidence(text, "angry refund", 2);
        assert_eq!(evidence.len(), 1);
        assert!(evidence[0].contains("Angry customer"));
    }

    #[test]
    fn returns_in_document_order() {
        let text = "Refund please. \
                    Some boring intro. \
                    Angry customer here. \
                    Another irrelevant note.";
        let evidence = text_evidence(text, "angry refund", 3);
        // Both "Refund please" and "Angry customer here" match; should
        // come back in original order regardless of which scored higher.
        assert_eq!(evidence.len(), 2);
        assert!(evidence[0].starts_with("Refund"));
        assert!(evidence[1].starts_with("Angry"));
    }

    #[test]
    fn no_match_returns_empty() {
        let evidence = text_evidence("Nothing to see here.", "xyz", 3);
        assert!(evidence.is_empty());
    }

    #[test]
    fn empty_inputs_safe() {
        assert!(text_evidence("", "x", 3).is_empty());
        assert!(text_evidence("x", "", 3).is_empty());
        assert!(text_evidence("hello world hello", "hello", 0).is_empty());
    }

    #[test]
    fn handles_newline_separated_text() {
        let text = "Line one with apples.\nLine two with oranges and apples.";
        let evidence = text_evidence(text, "apples", 3);
        assert_eq!(evidence.len(), 2);
    }
}
