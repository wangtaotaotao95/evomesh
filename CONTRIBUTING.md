# Contributing

Start with a reproducible task friction report: what the agent tried, what went wrong, and how to measure a better result. Remove private task data and credentials before sharing traces.

Use Python 3.11+, install `pip install -e '.[dev,mysql]'`, and run `ruff check .`, `ruff format --check .`, and `pytest -q`. Add a meaningful regression test for changes to proposal consistency, runtime behavior, or storage semantics. CI runs the same suite on SQLite and MySQL; use a disposable test database locally.

Keep pull requests focused. Include the behavior change, validation evidence, and deployment/schema implications. Evolving the evolution code is welcome; acceptance criteria should not be weakened merely to pass a candidate. Do not claim task-quality improvements without an evaluation.

The architecture is deliberately small. Discuss large dependencies, new storage backends, migrations, or changes to the proposal wire format before implementation. Contributions use the repository's MIT license.
