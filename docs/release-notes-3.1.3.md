# Nitro AI Judge CLI 3.1.3

3.1.3 fixes Play manager dashboard deletion controls and Jupyter kernel channels.

Rows with running or stopped containers now show Delete container behind a Yes/No confirmation. Once containers are gone, a preserved workspace shows Delete volume, while downloaded images can be removed independently.

The Jupyter reverse proxy now negotiates the client's WebSocket subprotocol with the upstream server before accepting the browser connection. This preserves Jupyter's binary framing for modern kernel-channel messages, which fallback images otherwise reject as text.
