# Owner round

## Renew the App Store provisioning certificate

The distribution certificate expires in January and renewal needs the
owner's Apple Developer login plus the keychain on the owner's machine.
No agent session can hold either. Renew the certificate, install the new
profile into the release pipeline, and record the new expiry date in the
ops note.

## Decide the paid model provider for client repositories

Before the first client repository is onboarded, the owner reads the
provider options memo end to end and decides: which provider, which
region, and what the data-processing agreement requires before any
client source reaches a model endpoint. Record the decision, its date
and the memo revision it was made against; the factory's configuration
follows from that record.
