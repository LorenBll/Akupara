# Security Policy

## Supported Versions

We only provide security updates for versions and deployments that are currently maintained by the project owner or release process. If this repository uses versioned releases, the supported versions should be listed here.

| Version | Supported |
| ------- | --------- |
| Latest   | Yes       |

## Reporting a Vulnerability

If you believe you have found a security issue, please report it privately to the project maintainers rather than opening a public issue.

Include as much detail as possible, such as:

- A clear description of the issue
- The affected component or feature
- Steps to reproduce the problem
- Any relevant logs, screenshots, or proof of concept code
- The potential impact and how severe you believe it is

If the report involves credentials, API keys, tokens, or other secrets, do not post them publicly. Redact sensitive values before sharing.

## What To Expect

After a report is received:

1. The issue will be reviewed and triaged.
2. You may be contacted for clarification or additional details.
3. A fix may be developed and validated before public disclosure.
4. The reporter may be credited unless they prefer to remain anonymous.

## Security Guidelines

This project is intended to follow basic security hygiene:

- Keep secrets out of source control.
- Use environment variables or local configuration files for sensitive values.
- Review third-party dependencies before adding them.
- Prefer the least-privilege deployment and runtime configuration that works for your use case.
- Treat all externally supplied input as untrusted and validate it before use.

## Disclosure Notes

Do not publicly disclose an unpatched vulnerability until maintainers have had reasonable time to investigate and respond. If a coordinated disclosure timeline is needed, it can be discussed during the report process.