#!/usr/bin/env bash
# Starts one vector store in a Kaggle notebook, where Docker is not available, for
# `load_bench.py --backend <store>` (see kaggle_stores.ipynb). Each store runs from its official
# release build, in a session of its own so that it outlives the notebook cell, and the script
# returns once the store answers. Milvus is not here: it needs Docker or Kubernetes.
#
#   bash kaggle_stores.sh qdrant|elasticsearch|opensearch|weaviate
set -euo pipefail

store="${1:?usage: kaggle_stores.sh qdrant|elasticsearch|opensearch|weaviate}"
root=/kaggle/working/stores
log="$root/$store.log"
mkdir -p "$root"
cd "$root"

fetch() { curl -sSfL "$1" | tar xz; }

start() { setsid nohup "$@" > "$log" 2>&1 < /dev/null & }

start_unprivileged() {
  # Elasticsearch and OpenSearch refuse to run as root.
  id -u search > /dev/null 2>&1 || useradd -m search
  chown -R search "$1"
  setsid nohup su search -s /bin/bash -c "$2" > "$log" 2>&1 < /dev/null &
}

case "$store" in
  qdrant)
    fetch https://github.com/qdrant/qdrant/releases/download/v1.19.2/qdrant-x86_64-unknown-linux-gnu.tar.gz
    export QDRANT__STORAGE__STORAGE_PATH="$root/qdrant-data"
    start ./qdrant
    ready=http://127.0.0.1:6333/readyz
    ;;
  elasticsearch)
    fetch https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-9.5.5-linux-x86_64.tar.gz
    start_unprivileged elasticsearch-9.5.5 "ES_JAVA_OPTS='-Xms8g -Xmx8g' $root/elasticsearch-9.5.5/bin/elasticsearch \
      -Ediscovery.type=single-node -Expack.security.enabled=false -Expack.ml.enabled=false"
    ready="http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=1s"
    ;;
  opensearch)
    fetch https://artifacts.opensearch.org/releases/bundle/opensearch/3.9.0/opensearch-3.9.0-linux-x64.tar.gz
    echo "plugins.security.disabled: true" >> opensearch-3.9.0/config/opensearch.yml
    start_unprivileged opensearch-3.9.0 "OPENSEARCH_JAVA_OPTS='-Xms8g -Xmx8g' $root/opensearch-3.9.0/bin/opensearch \
      -Ediscovery.type=single-node"
    ready="http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=1s"
    ;;
  weaviate)
    fetch https://github.com/weaviate/weaviate/releases/download/v1.39.12/weaviate-v1.39.12-linux-amd64.tar.gz
    export AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED=true PERSISTENCE_DATA_PATH="$root/weaviate-data"
    export DEFAULT_VECTORIZER_MODULE=none CLUSTER_HOSTNAME=node1
    start ./weaviate --host 127.0.0.1 --port 8080 --scheme http
    ready=http://127.0.0.1:8080/v1/.well-known/ready
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
