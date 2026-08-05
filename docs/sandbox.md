# Infrastructure sandbox

The original project includes a local restaurant application stack, nine
infrastructure adapters, and ten failure injection scripts. These examples
exercise the incident/remediation pipeline and require system-specific setup.

Requires Docker, kind, kubectl, and Python 3.11+.

```bash
kind create cluster --name slopsaver --config sandbox/kind-config.yaml
docker build -t slopsaver-sandbox:latest sandbox/
kind load docker-image slopsaver-sandbox:latest --name slopsaver
kubectl apply -f sandbox/k8s/all.yaml

python -m pip install '.[adapters,sandbox]'
cp config/customer.example.yaml config/customer.yaml
ai-sre run --fake-reasoner
```

The customer example starts in alert-only mode. In a second terminal:

```bash
python -m sandbox.chaos.spam_forms
python -m sandbox.chaos.exhaust_db_pool
python -m sandbox.chaos.flip_s3_policy
```

Inspect `var/incidents/`. To exercise remediation, review the adapter actions,
then use `config/demo.yaml` for the local sandbox (it enables autonomous
behavior). P1 actions wait in the approval queue:

```bash
ai-sre run --config config/demo.yaml --fake-reasoner
ai-sre pending --config config/demo.yaml
ai-sre approve INCIDENT_ID --config config/demo.yaml --fake-reasoner
ai-sre dashboard --config config/demo.yaml
```

Stripe needs its own key and order-matching configuration. Storage policies,
database connections, registrar data, TLS hooks, backup restore tools, and HTTP
admin endpoints have integration-specific dependencies; inspect adapter
docstrings before enabling them outside the sandbox.

For AI reasoning, install `.[ai]`, set `ANTHROPIC_API_KEY`, and omit
`--fake-reasoner`. Unit and golden tests need neither a cluster nor a model API.
