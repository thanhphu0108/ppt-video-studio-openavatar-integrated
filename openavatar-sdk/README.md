# OpenAvatar SDK

Clients for OpenAvatar Runtime.

## Python

```python
from openavatar_sdk import OpenAvatarClient

client = OpenAvatarClient()
print(client.health())

job = client.generate_avatar("avatar.jpg", "voice.wav")
result = client.wait(job["id"])

if result["status"] == "completed":
    client.download(job["id"], "result.mp4")
```

## Browser

```javascript
import { OpenAvatarClient } from "./openavatar-client.js";

const client = new OpenAvatarClient();
const health = await client.health();
```

The browser client is intended for Streamlit Cloud browser bridges.
