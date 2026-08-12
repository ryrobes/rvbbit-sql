ARG ORACLE_BASE_IMAGE=gvenzl/oracle-free:23.26.2-slim-faststart@sha256:d8913e4e4769b6e60197949bef30a4391713afe662b4b4e71a2665c881bdac8b
FROM ${ORACLE_BASE_IMAGE}

ARG ORACLE_SAMPLE_REVISION=6660bad68c07bd143430ace58565b3f727e17263
ARG ORACLE_SAMPLE_ARCHIVE_SHA256=e5365e55840dd1f23a712704951e6d77f928bb0100807781576ff92fa1cff6ad

USER root

RUN set -eux; \
    archive=/tmp/db-sample-schemas.tar.gz; \
    curl --fail --location --silent --show-error \
      "https://github.com/oracle-samples/db-sample-schemas/archive/${ORACLE_SAMPLE_REVISION}.tar.gz" \
      --output "${archive}"; \
    echo "${ORACLE_SAMPLE_ARCHIVE_SHA256}  ${archive}" | sha256sum --check -; \
    mkdir -p /opt/rvbbit-samples; \
    tar --extract --gzip --file "${archive}" \
      --directory /opt/rvbbit-samples --strip-components=1 \
      "db-sample-schemas-${ORACLE_SAMPLE_REVISION}/customer_orders" \
      "db-sample-schemas-${ORACLE_SAMPLE_REVISION}/sales_history"; \
    awk '/^SET LOAD / { exit } { print }' \
      /opt/rvbbit-samples/sales_history/sh_populate.sql \
      > /opt/rvbbit-samples/sales_history/sh_populate_before_load.sql; \
    awk 'emit { print } /^LOAD supplementary_demographics supplementary_demographics.csv/ { emit=1 }' \
      /opt/rvbbit-samples/sales_history/sh_populate.sql \
      > /opt/rvbbit-samples/sales_history/sh_populate_after_load.sql; \
    sed -i '/^CREATE INDEX sup_text_idx /,+1d' \
      /opt/rvbbit-samples/sales_history/sh_populate_after_load.sql; \
    rm -f "${archive}"; \
    chown -R oracle:oinstall /opt/rvbbit-samples

COPY --chown=oracle:oinstall rvbbit-init.sh /opt/rvbbit-samples/rvbbit-init.sh
COPY --chown=oracle:oinstall loader/ /opt/rvbbit-samples/loader/

RUN chmod 0755 /opt/rvbbit-samples/rvbbit-init.sh

LABEL org.opencontainers.image.source="https://github.com/rvbbit/rvbbit-sql" \
      rvbbit.fixture="oracle-sample-schemas" \
      rvbbit.fixture.oracle-version="23.26.2" \
      rvbbit.fixture.sample-revision="6660bad68c07bd143430ace58565b3f727e17263"

USER oracle
