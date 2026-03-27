FROM neo4j:5.24-enterprise

ENV NEO4J_apoc_export_file_enabled=true \
    NEO4J_apoc_import_file_enabled=true \
    NEO4J_apoc_import_file_use__neo4j__config=true \
    NEO4JLABS_PLUGINS='["apoc"]'

VOLUME /data /plugins /import

COPY neo4j.dump /import/neo4j.dump

# RUN bin/neo4j-admin database load --from-path=/import neo4j --overwrite-destination=true
