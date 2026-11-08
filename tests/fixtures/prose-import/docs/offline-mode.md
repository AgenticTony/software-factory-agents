# Offline mode

## Work order: offline mode for the field kit

The field kit must work a full day with no network. This is the largest
item in the plan: it touches storage, synchronisation, the interface and
the release process, and it cannot land as one task — plan it as several,
in dependency order.

The work:

1. A local cache layer over the storage client: reads hit the cache
   first, writes go to the cache and are marked pending, and the cache
   has a size cap with least-recently-used eviction that never evicts a
   record with pending writes.
2. A sync engine that replays every pending mutation against the remote
   when the network returns, from a durable queue that survives process
   restarts and device shutdowns mid-replay.
3. Conflict resolution: last-write-wins per record with a per-record
   audit trail; a conflicting edit is never silently dropped, and the
   loser is kept in the audit trail with both timestamps.
4. The interface: an offline banner that tells the truth (offline, or
   online with pending writes), per-record sync state, and a manual
   "sync now" control that shows replay progress.
5. Migration of existing local data onto the cache schema: a one-way
   upgrade that refuses to run twice, with a written rollback note for
   the release channel.
6. Telemetry: offline session counts, sync lag after reconnection, and
   conflict rates, all visible on the existing dashboard.
7. Documentation: the offline chapter of the handbook, written against
   the shipped behaviour, not the plan.
8. A rehearsal: one full working day of airplane-mode dogfooding by the
   team, with the sync log kept as evidence, before the release is
   announced.
