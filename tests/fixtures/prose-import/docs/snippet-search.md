# Snippet search

## Work order: paste-with-preview in the snippet search

Today, pasting a long block into the search box fires a query per
keystroke and the results flash while the paste settles. Build the paste
path the way the queue page already behaves: hold the query until the
paste lands, then run it once.

The preview panel under the box should show the first three matches with
their project and line, rendered exactly as the results page renders
them, so a paste can be judged before committing to a full search.

### How we'll know it works

Pasting a twenty-line block runs exactly one query — count them in the
network log. The preview shows three matches within a second of the
paste landing. Typing without pasting behaves exactly as before, and a
paste into an empty box still finds nothing rather than inventing
results.
