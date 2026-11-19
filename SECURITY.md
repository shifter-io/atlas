# Security policy

## Reporting a vulnerability

Please report security problems privately, through GitHub's private vulnerability reporting on this repository: open the **Security** tab and choose **Report a vulnerability**. Private vulnerability reporting is enabled when the repository goes public.

Do not open a public issue, pull request or discussion for a security problem.

Please include what you found, how to reproduce it, and the impact you expect. We will confirm receipt, keep you informed while we work on a fix, and credit you when the fix is published, unless you prefer to stay anonymous.

## Scope

- The code in this repository: the Python test framework and CLI, the scoring, and the Go API.
- The public Atlas API at `atlas.shifter.io`.

Out of scope: denial-of-service tests, automated scanning that generates high traffic, social engineering, and the third-party proxy networks Atlas measures (report those to their providers).

## Supported versions

Security fixes are made on the `main` branch.
