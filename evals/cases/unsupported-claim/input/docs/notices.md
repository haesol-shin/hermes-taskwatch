# Notices

Sync reads the notice board at `https://board.example.invalid/notices`.

## Fetching

Sync lists the notices, then fetches the body of each notice that is new since the last run.

## Read state

Opening the notice list already marks every listed notice as read on the board, so opening a detail page changes nothing that the list did not already change. Sync therefore opens the detail page of every new notice without asking.
