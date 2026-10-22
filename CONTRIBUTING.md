# Contributing

Issues and pull requests are welcome. Describe the behaviour you expected, what happened, your Python version and the smallest safe reproduction. Use synthetic locations and observations; remove keys, project identifiers and personal route data from logs and screenshots.

## Local checks

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-cloud.txt
python -m unittest discover -s tests -v
python -m traveltime --db demo-output/review.sqlite3 --output demo-output/review demo --days 5
```

Use a fresh database path for each demo. Tests and the demo should not need credentials, call a paid API or create cloud resources. Inspect the generated HTML when changing report rendering.

Keep scheduling, request accounting and stored-data compatibility explicit. Changes to collection behaviour should include focused tests for the affected edge cases. A failed, late or missing observation must not become a fabricated successful sample. Document new configuration keys in both the example and the configuration guide.

For a pull request, explain the problem, the change and how you checked it. Mention migration steps when a configuration or database format changes. Do not include generated real reports, databases, credentials or personal configuration. Project-authored contributions are distributed under the [MIT license](LICENSE); external API data keeps its provider's terms.
