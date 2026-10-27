# Project instructions

- Do not compile or build this project locally. Run frontend compilation,
  production builds, and Docker image builds on the explicitly authorized remote
  builder. The current user authorizes the existing InstaBox compile-server
  instead of GitHub Actions; follow ops/instabox/AGENTS.md and the machine rules.
- Local source inspection, formatting, and tests that do not compile the project
  are allowed. Run frontend component and editor regression tests on that remote builder.
- When creating a lightweight sub-agent, use `gpt-6-luna`.
