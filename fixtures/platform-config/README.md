# platform-config fixture

Database/configuration consumer of the account field. `schema.sql` contains the
legacy `customer_id` identifiers that must be migrated to `account_id` alongside
Python consumers. Its regression test preserves the semantic assertion that the
legacy key is absent after migration.
