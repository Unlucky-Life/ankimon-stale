# Ankimon Mobile Demo Deck

This is a tiny proof of concept for playing a score-based Ankimon game inside
AnkiMobile on iOS. Each card uses the standard `Basic` note type, an Ankimon
battle-scene background, and a Pokémon sprite. Tap **ATTACK** before revealing
the answer to add 10 points. The score is kept in the card web view's
`localStorage`, so it persists while studying on the device. Rate the card
normally afterward so Anki can schedule it.

## Import

Import `Ankimon-Mobile-Demo.apkg` into AnkiMobile. The source YAML is kept next
to it so the cards can be regenerated with `anki-cli-unofficial` in a
compatible Python/Anki environment:

```powershell
${env:PYTHONPATH} = 'C:\path\to\anki-cli'
py -3.12 C:\path\to\anki-cli\scripts\anki-cli-unofficial `
  load --media-dir .\media --deck Default `
  .\cards.yaml .\demo.apkg
```

The checked-in package was post-processed into the `Ankimon Mobile Demo`
deck name; import it directly for the ready-to-try version.

This is intentionally local-only: it proves the mobile card-template game loop
before adding a remote score API or multiplayer synchronization.
