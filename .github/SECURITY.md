# Security Policy

Please report vulnerabilities privately through
[GitHub's vulnerability reporting](https://github.com/johanubbink/klein-bt/security/advisories/new),
not in a public issue.

## What klein exposes

klein only watches. It asks the robot's Groot2 publisher for data and turns
on its transition recording, and nothing a dashboard sends reaches the robot.
What it serves can still be sensitive. The dashboard shows the live tree and
every blackboard value, and `/log.zip` downloads the whole recording.

- **By default it listens on `127.0.0.1` only**, so only this machine can open
  the dashboard.
- **`--host 0.0.0.0` (or another address) opens it to the network, with no
  authentication.** Anyone who can reach the port can read everything above.
  Use it only on a trusted network, or reach a remote klein through an SSH
  tunnel instead.
- **The WebSocket refuses other sites' pages.** A browser lets any page open a
  WebSocket to `localhost`, so `/ws` only upgrades when the request's
  `Origin` names the host and port it was sent to. Clients that aren't
  browsers send no `Origin` and are let through.
- **The plain-HTTP side (the page and the `/log*` downloads) has no origin
  check.** A browser stops other sites from reading those responses, but a
  DNS-rebinding page could get around that. Klein doesn't defend against it.
