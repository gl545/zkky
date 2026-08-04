# Contributing

## Development setup

```bat
copy standalone_config.example.json standalone_config.json
start_standalone.cmd
```

## Before submitting

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
.venv\Scripts\python.exe scripts\release_check.py
```

Contributions should:

1. keep the service bound to the loopback interface by default;
2. keep tests offline and deterministic;
3. add explicit timeouts to every network request;
4. redact credentials and payment data from errors and logs;
5. avoid replaying mutating requests after ambiguous transport failures;
6. verify final account state instead of equating a button click or HTTP 200
   response with success.

Use synthetic values in tests. Do not commit local configuration, captures,
runtime fingerprint data, real account identifiers, proxy credentials, or
payment information.
