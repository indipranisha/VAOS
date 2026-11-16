# VAOS project overview

VAOS (Video Analytics Operating System) is a local-first application for camera-based object detection, approximate tracking, human-reviewed samples, dataset preparation, custom model training and optional face enrollment/matching. The browser dashboard and camera station use a local FastAPI service pair; SQLite and model/data files stay on the configured VAOS host unless an administrator configures an external tunnel or deployment.

## Creator and community

VAOS was originally created by Prateek Dubey and is developed with contributions from the open-source community. See [creator and community credits](CREDITS.md) for the creator's role and profile links, or use the in-app **About VAOS** page.

The creator's public links are [prateekdubey.in](https://prateekdubey.in) and [LinkedIn](https://www.linkedin.com/in/progressiveprateek). For an overview suitable for project collaborators, see the [VAOS white paper](VAOS_WHITE_PAPER.md).

The application is licensed under Apache 2.0. Contributors retain appropriate attribution and third-party models, datasets and dependencies retain their own license terms. See the [contribution guide](../CONTRIBUTING.md) and [license](../LICENSE).

## Release status

The working source reports version 1.1.0. The full v1.1.0 feature scope is still under development; see the [current project status](PROJECT_STATUS.md). A listed source feature is not automatically production-validated. Staging remains gated on regression, security, usability, migration and performance evidence.

## Start here

- Operators: [README installation and deployment](../README.md)
- Integrators: [REST API](API.md) and [MCP bridge](MCP.md)
- Developers: [architecture](ARCHITECTURE.md), [testing](TESTING.md), and [contributing](../CONTRIBUTING.md)
- Operators and integrators: [Observability & Analytics Centre](observability/overview.md), [metrics](observability/metrics-dictionary.md), and [CLI](observability/cli-reference.md)
