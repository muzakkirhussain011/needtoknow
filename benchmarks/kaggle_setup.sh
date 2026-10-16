#!/usr/bin/env bash
# Installs PostgreSQL and builds pgvector inside a Kaggle notebook (Ubuntu, running as root),
# then creates the database the benchmark uses: postgresql://bench:bench@localhost/bench
set -euo pipefail

PGVECTOR_VERSION="${PGVECTOR_VERSION:-v0.8.7}"
export DEBIAN_FRONTEND=noninteractive

apt-get -qq update
apt-get -qq install -y postgresql > /dev/null
PG="$(ls /usr/lib/postgresql | sort -V | tail -1)"
apt-get -qq install -y "postgresql-server-dev-${PG}" build-essential git > /dev/null

rm -rf /tmp/pgvector
git clone -q --depth 1 --branch "${PGVECTOR_VERSION}" https://github.com/pgvector/pgvector.git /tmp/pgvector
make -s -C /tmp/pgvector PG_CONFIG="/usr/lib/postgresql/${PG}/bin/pg_config" > /dev/null
make -s -C /tmp/pgvector PG_CONFIG="/usr/lib/postgresql/${PG}/bin/pg_config" install > /dev/null

service postgresql start
su postgres -c "psql -q -c \"DROP DATABASE IF EXISTS bench\""
su postgres -c "psql -q -c \"DROP ROLE IF EXISTS bench\""
su postgres -c "psql -q -c \"CREATE ROLE bench WITH LOGIN SUPERUSER PASSWORD 'bench'\""
su postgres -c "createdb -O bench bench"
echo "PostgreSQL ${PG} with pgvector ${PGVECTOR_VERSION} is running"
