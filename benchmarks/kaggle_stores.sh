#!/usr/bin/env bash
# Starts one vector store in a Kaggle notebook, where Docker is not available, for
# `load_bench.py --backend <store>` (see kaggle_stores.ipynb). Each store runs from its official
# release build, in a session of its own so that it outlives the notebook cell, and the script
# returns once the store answers. `stop` ends the running store and deletes its data, so the next
# one starts on a free port with free memory. Milvus is not here: it needs Docker or Kubernetes.
# LanceDB is embedded, so `lancedb` only makes the directory its table goes in. Redis comes from
# the packages.redis.io APT repository, as Redis Open Source 8 with its Query Engine.
#
#   bash kaggle_stores.sh qdrant|elasticsearch|opensearch|weaviate|lancedb|redis|stop
set -euo pipefail

store="${1:?usage: kaggle_stores.sh qdrant|elasticsearch|opensearch|weaviate|lancedb|redis|stop}"
# The stores' files stay out of /kaggle/working, which Kaggle saves as the notebook's output;
# only their logs are kept there.
root=/tmp/needtoknow-stores
mkdir -p "$root"
cd "$root"

if [ "$store" = stop ]; then
  for pidfile in "$root"/*.pid; do
    [ -e "$pidfile" ] || continue
    # Each store runs in its own session, so its process group holds what it started...
    kill -- -"$(cat "$pidfile")" 2> /dev/null || true
    rm -f "$pidfile"
  done
  # ...except that su may start Elasticsearch and OpenSearch in a session of their own; their
  # command lines name their directory under $root.
  pkill -f "$root/" 2> /dev/null || true
  pkill -x qdrant 2> /dev/null || true
  pkill -x weaviate 2> /dev/null || true
  pkill -x redis-server 2> /dev/null || true
  for _ in $(seq 1 60); do
    pgrep -f "$root" > /dev/null || pgrep -x qdrant > /dev/null || pgrep -x weaviate > /dev/null \
      || pgrep -x redis-server > /dev/null || break
    sleep 1
  done
  rm -rf "$root"/*-data "$root"/elasticsearch-*/data "$root"/opensearch-*/data
  echo "stopped"
  exit 0
fi

log="/kaggle/working/$store.log"

fetch() { curl -sSfL "$1" | tar xz; }

start() {
  setsid nohup "$@" > "$log" 2>&1 < /dev/null &
  echo $! > "$root/$store.pid"
}

start_unprivileged() {
  # Elasticsearch and OpenSearch refuse to run as root.
  id -u search > /dev/null 2>&1 || useradd -m search
  chown -R search "$1"
  setsid nohup su search -s /bin/bash -c "$2" > "$log" 2>&1 < /dev/null &
  echo $! > "$root/$store.pid"
}

case "$store" in
  qdrant)
    [ -x qdrant ] || fetch https://github.com/qdrant/qdrant/releases/download/v1.19.2/qdrant-x86_64-unknown-linux-gnu.tar.gz
    export QDRANT__STORAGE__STORAGE_PATH="$root/qdrant-data"
    start ./qdrant
    ready=http://127.0.0.1:6333/readyz
    ;;
  elasticsearch)
    [ -d elasticsearch-9.5.5 ] ||
      fetch https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-9.5.5-linux-x86_64.tar.gz
    start_unprivileged elasticsearch-9.5.5 "ES_JAVA_OPTS='-Xms8g -Xmx8g' $root/elasticsearch-9.5.5/bin/elasticsearch \
      -Ediscovery.type=single-node -Expack.security.enabled=false -Expack.ml.enabled=false"
    ready="http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=1s"
    ;;
  opensearch)
    if [ ! -d opensearch-3.9.0 ]; then
      fetch https://artifacts.opensearch.org/releases/bundle/opensearch/3.9.0/opensearch-3.9.0-linux-x64.tar.gz
      echo "plugins.security.disabled: true" >> opensearch-3.9.0/config/opensearch.yml
    fi
    start_unprivileged opensearch-3.9.0 "OPENSEARCH_JAVA_OPTS='-Xms8g -Xmx8g' $root/opensearch-3.9.0/bin/opensearch \
      -Ediscovery.type=single-node"
    ready="http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=1s"
    ;;
  weaviate)
    [ -x weaviate ] || fetch https://github.com/weaviate/weaviate/releases/download/v1.39.12/weaviate-v1.39.12-linux-amd64.tar.gz
    export AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED=true PERSISTENCE_DATA_PATH="$root/weaviate-data"
    export DEFAULT_VECTORIZER_MODULE=none CLUSTER_HOSTNAME=node1
    start ./weaviate --host 127.0.0.1 --port 8080 --scheme http
    ready=http://127.0.0.1:8080/v1/.well-known/ready
    ;;
  lancedb)
    mkdir -p "$root/lancedb-data"
    echo "lancedb is embedded; its table goes in $root/lancedb-data"
    exit 0
    ;;
  redis)
    if ! command -v redis-server > /dev/null || ! redis-server --version | grep -q "v=8"; then
      . /etc/os-release
      curl -fsSL https://packages.redis.io/gpg | gpg --dearmor --yes -o /usr/share/keyrings/redis-archive-keyring.gpg
      echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb $VERSION_CODENAME main" \
        > /etc/apt/sources.list.d/redis.list
      apt-get update -qq > /dev/null
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq redis > /dev/null
    fi
    redis-server --version
    mkdir -p "$root/redis-data"
    config=/etc/redis/redis.conf
    [ -e "$config" ] || config=
    # Load the bundled modules if the package's config does not load them itself.
    modules=()
    if ! grep -qs "^loadmodule" "$config"; then
      for so in /usr/lib/redis/modules/*.so; do
        if [ -e "$so" ]; then modules+=(--loadmodule "$so"); fi
      done
    fi
    start redis-server $config "${modules[@]}" --daemonize no --supervised no --bind 127.0.0.1 --port 6379 \
      --protected-mode no --dir "$root/redis-data" --save "" --appendonly no --logfile ""
    for _ in $(seq 1 60); do
      redis-cli ping 2> /dev/null | grep -q PONG && break
      sleep 1
    done
    # The benchmark needs the Query Engine: fail here, with the log, if this build lacks it.
    if ! redis-cli FT._LIST > /dev/null 2>&1 || redis-cli FT._LIST 2>&1 | grep -qi "unknown command"; then
      echo "redis started without its Query Engine; the end of $log:" >&2
      tail -n 40 "$log" >&2
      exit 1
    fi
    echo "redis is up"
    exit 0
    ;;
  *)
    echo "unknown store: $store" >&2
    exit 2
    ;;
esac

for _ in $(seq 1 180); do
  if curl -sf "$ready" > /dev/null; then
    echo "$store is up"
    exit 0
  fi
  sleep 2
done
echo "$store did not start; the end of $log:" >&2
tail -n 40 "$log" >&2
exit 1
