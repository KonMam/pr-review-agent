# Mock: pr-review's PR output (item 8b)

Built from go-taskqueue#2's real findings. The text is the model's claim as written; `tests/test_post_format.py` holds the same output as snapshots.

---

## Inline comment on `api/server.go` line 61

🐛 **Ignored error**

**The return value of json.NewEncoder(w).Encode(task) is now discarded.**

When encoding fails (e.g. an unencodable field or invalid raw JSON), the handler silently returns a 200 with a truncated or empty body instead of the previous 500 error, so the client cannot tell the response is broken.

---

## Summary review: every finding is inline (the usual case)

**pr-review** found 3 possible bugs, commented inline.

<sub>deepseek/deepseek-v4.1-flash · $0.0032</sub>

---

## Summary review: one finding could not go inline

A finding on lines outside the diff can't be an inline comment, so the summary carries it in full. The example is invented to show the layout.

**pr-review** found 3 possible bugs: 2 commented inline, 1 outside the diff.

🐛 **Nil dereference** · [`store/store.go:40-42`](https://github.com/KonMam/go-taskqueue/blob/HEAD_SHA/store/store.go#L40-L42)

**`t` can be nil when the lookup misses.**

`Get` returns `nil, nil` for an unknown id, and the caller reads `t.Status` without a check.

<sub>deepseek/deepseek-v4.1-flash · $0.0032</sub>
