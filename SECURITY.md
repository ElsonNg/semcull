# Security

Selected source and intent are sent to TypeSafe Jev. Semcull does not redact
source secrets or make semantic decisions safe for irreversible operations.
Keep deterministic authorization checks outside the classifier.

The temporary store uses private permissions and rejects symlink traversal in
managed directories. It is not encrypted, power-loss durable, or a secure erase
facility. Processes running as the same user can access it. Count-only retention
does not prevent disk exhaustion, and concurrent deletion can invalidate results.

Never put API keys, private captures, or full provider responses in public bug
reports. Before public release, configure a private vulnerability-reporting
channel in the hosting repository. No hosted reporting channel exists yet.
