#!/usr/bin/env bash
# Rebuild every golden-set case from its fix commit. Needs network (to clone)
# and is NOT part of verify.sh: the test checks the committed output offline.
#
#   cases/build.sh            clone missing repos into cache/repos, rebuild all
#
# Each line: id, provenance repo, commit sha, licence, bug class, truth hunks
# ("-" = all). See `make_case --list-hunks` for hunk indices. Class `clean`
# builds a clean case (`make_case --clean`): the commit as it was, no bugs,
# checked against its next 20 commits.
# Own repos are read from ~/projects and never written.
set -euo pipefail
cd "$(dirname "$0")/.."

CACHE=cache/repos
OWN="$HOME/projects"
mkdir -p "$CACHE"
# Start clean so a case dropped from the list leaves no folder behind.
rm -f cases/manifest.jsonl
find cases -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +

dir_for() { # provenance repo -> local clone
  case "$1" in
    github.com/KonMam/*) echo "$OWN/${1##*/}" ;;
    github.com/*) local r=${1#github.com/}; echo "$CACHE/${r/\//_}" ;;
  esac
}

while read -r id repo sha licence class truth; do
  [[ -z "$id" || "$id" == \#* ]] && continue
  dir=$(dir_for "$repo")
  if [[ ! -d "$dir" ]]; then
    git clone -q --no-checkout --single-branch "https://$repo" "$dir"
  fi
  args=(--repo-dir "$dir" --fix "$sha" --id "$id" --repo "$repo" --licence "$licence")
  if [[ "$class" == clean ]]; then
    args+=(--clean)
  else
    args+=(--bug-class "$class")
    [[ "$truth" != "-" ]] && args+=(--truth "$truth")
  fi
  uv run -q python -m pr_review.make_case "${args[@]}" >/dev/null
  # Keep the upstream licence text next to the copied files (attribution).
  # A bug case's fix commit wins; a clean case only fills a missing one.
  lic=$(git -C "$dir" ls-tree --name-only "$sha" | grep -i -m1 -E '^(licen[cs]e|copying)' || true)
  out="cases/licences/${repo#github.com/}.txt"
  if [[ -n "$lic" && ! ( "$class" == clean && -f "$out" ) ]]; then
    mkdir -p "$(dirname "$out")"
    git -C "$dir" show "$sha:$lic" > "$out"
  fi
  echo "built $id"
done <<'EOF'
# --- owner's repos (go-taskqueue is public, no licence file) ---
go-taskqueue-aac3b48   github.com/KonMam/go-taskqueue       aac3b48   none                ignored_err          1,2,6
go-taskqueue-e75bae4   github.com/KonMam/go-taskqueue       e75bae4   none                ignored_err          2
go-taskqueue-f8d74eb   github.com/KonMam/go-taskqueue       f8d74eb   none                data_race            4,6
# --- goroutine leaks and missing cancellation ---
echo-e213332           github.com/labstack/echo             e213332   MIT                 goroutine_leak       -
kafka-go-ed22f4e       github.com/segmentio/kafka-go        ed22f4e   MIT                 goroutine_leak       -
nats-fa6ece7           github.com/nats-io/nats.go           fa6ece7   Apache-2.0          goroutine_leak       -
nats-7d27136           github.com/nats-io/nats.go           7d27136   Apache-2.0          goroutine_leak       -
sarama-9f8650a         github.com/IBM/sarama                9f8650a   MIT                 goroutine_leak       3,4,5,6
# --- data races and mutex misuse ---
sarama-a8b3b3d         github.com/IBM/sarama                a8b3b3d   MIT                 data_race            -
badger-32a2548         github.com/dgraph-io/badger          32a2548   Apache-2.0          data_race            -
gin-1bd0ecf            github.com/gin-gonic/gin             1bd0ecf   MIT                 data_race            -
prometheus-59c00e3     github.com/prometheus/client_golang  59c00e3   Apache-2.0          data_race            -
# --- nil dereferences and nil maps ---
chi-1c2d011            github.com/go-chi/chi                1c2d011   MIT                 nil_deref            -
nats-654ca4e           github.com/nats-io/nats.go           654ca4e   Apache-2.0          nil_deref            -
cli-e855c4c            github.com/urfave/cli                e855c4c   MIT                 nil_deref            -
gin-e0d46de            github.com/gin-gonic/gin             e0d46de   MIT                 nil_deref            -
echo-05df10c           github.com/labstack/echo             05df10c   MIT                 nil_deref            1,2,4,5
# --- defer inside loops ---
pgx-ee304a8d           github.com/jackc/pgx                 ee304a8d  MIT                 defer_in_loop        -
badger-90c3fbf         github.com/dgraph-io/badger          90c3fbf   Apache-2.0          defer_in_loop        -
# --- slice aliasing after append ---
go-redis-9b33d16       github.com/redis/go-redis            9b33d16   BSD-2-Clause        slice_aliasing       -
fasthttp-3aa09fe       github.com/valyala/fasthttp          3aa09fe   MIT                 slice_aliasing       -
go-redis-4767d9d       github.com/redis/go-redis            4767d9d   BSD-2-Clause        slice_aliasing       1
badger-7ceb365         github.com/dgraph-io/badger          7ceb365   Apache-2.0          slice_aliasing       -
# --- wrong context propagation ---
echo-02de901           github.com/labstack/echo             02de901   MIT                 context_propagation  2
go-redis-da69ba0       github.com/redis/go-redis            da69ba0   BSD-2-Clause        context_propagation  -
kafka-go-b0b1cdc       github.com/segmentio/kafka-go        b0b1cdc   MIT                 context_propagation  -
sarama-05cb9fa         github.com/IBM/sarama                05cb9fa   MIT                 context_propagation  -
# --- integer overflow and off-by-one ---
badger-29cea8d         github.com/dgraph-io/badger          29cea8d   Apache-2.0          overflow_off_by_one  -
groupcache-3f89d3e     github.com/golang/groupcache         3f89d3e   Apache-2.0          overflow_off_by_one  -
gin-7cb151b            github.com/gin-gonic/gin             7cb151b   MIT                 overflow_off_by_one  -
go-redis-f92951b       github.com/redis/go-redis            f92951b   BSD-2-Clause        overflow_off_by_one  -
badger-89689ef         github.com/dgraph-io/badger          89689ef   Apache-2.0          overflow_off_by_one  0,3
# --- unclosed resources ---
gin-c3d5a28            github.com/gin-gonic/gin             c3d5a28   MIT                 unclosed_resource    -
gin-aabaccb            github.com/gin-gonic/gin             aabaccb   MIT                 unclosed_resource    -
prometheus-08baa43     github.com/prometheus/client_golang  08baa43   Apache-2.0          unclosed_resource    -
fasthttp-a1fca0f       github.com/valyala/fasthttp          a1fca0f   MIT                 unclosed_resource    1
groupcache-38eed8b     github.com/golang/groupcache         38eed8b   Apache-2.0          unclosed_resource    -
# --- ignored or shadowed err ---
echo-f7dc94d           github.com/labstack/echo             f7dc94d   MIT                 ignored_err          -
prometheus-b09d588     github.com/prometheus/client_golang  b09d588   Apache-2.0          ignored_err          -
kafka-go-51b35f2       github.com/segmentio/kafka-go        51b35f2   MIT                 ignored_err          -
bbolt-53a930f          github.com/etcd-io/bbolt             53a930f   MIT                 ignored_err          -
# --- clean: ordinary features and refactors, no known bug (own repos: none
# --- has 20 later commits that leave a candidate's lines alone) ---
echo-clean-09ccfba     github.com/labstack/echo             09ccfba   MIT                 clean                -
gin-clean-53410d2      github.com/gin-gonic/gin             53410d2   MIT                 clean                -
gin-clean-96ece6a      github.com/gin-gonic/gin             96ece6a   MIT                 clean                -
chi-clean-1576723      github.com/go-chi/chi                1576723   MIT                 clean                -
go-redis-clean-2bae9fe github.com/redis/go-redis            2bae9fe   BSD-2-Clause        clean                -
go-redis-clean-e6ea2d2 github.com/redis/go-redis            e6ea2d2   BSD-2-Clause        clean                -
sarama-clean-d39f8ea   github.com/IBM/sarama                d39f8ea   MIT                 clean                -
sarama-clean-e76f927   github.com/IBM/sarama                e76f927   MIT                 clean                -
nats-clean-b5bc7b4     github.com/nats-io/nats.go           b5bc7b4   Apache-2.0          clean                -
prometheus-clean-b3886a6 github.com/prometheus/client_golang b3886a6  Apache-2.0          clean                -
prometheus-clean-572ca3a github.com/prometheus/client_golang 572ca3a  Apache-2.0          clean                -
kafka-go-clean-ceb95ed github.com/segmentio/kafka-go        ceb95ed   MIT                 clean                -
cli-clean-c344b46      github.com/urfave/cli                c344b46   MIT                 clean                -
fasthttp-clean-7aea379 github.com/valyala/fasthttp          7aea379   MIT                 clean                -
badger-clean-10a09e6   github.com/dgraph-io/badger          10a09e6   Apache-2.0          clean                -
badger-clean-a3882a0   github.com/dgraph-io/badger          a3882a0   Apache-2.0          clean                -
bbolt-clean-287049e    github.com/etcd-io/bbolt             287049e   MIT                 clean                -
bbolt-clean-2d788aa    github.com/etcd-io/bbolt             2d788aa   MIT                 clean                -
pgx-clean-0aebf611     github.com/jackc/pgx                 0aebf611  MIT                 clean                -
pgx-clean-ac1ef1d1     github.com/jackc/pgx                 ac1ef1d1  MIT                 clean                -
EOF
