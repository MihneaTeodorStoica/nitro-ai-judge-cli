# Nitro AI Judge CLI 3.1.1

3.1.1 fixes the manager's Remove images action so it deletes every present competition image candidate, including fallback refs, and the row now drops to missing once nothing is left.

Delete workspace continues to preserve saved image metadata, so fallback-backed competitions don't falsely look like they lost images after workspace cleanup.
