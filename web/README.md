# Gawah dispute console

The System 3 console shows PayPal Sandbox disputes, linked payment references, status history, and sync results.

1. Start the [System 3 backend](../system3/counsel/README.md) on port 8002.
2. In `web/`, copy `.env.example` to `.env.local` and set `SYSTEM3_KEY` to the backend's value. The Next.js server uses `SYSTEM3_API_URL` to forward requests to FastAPI.
3. Run the following with Node.js 20.19+.

```sh
npm ci
npm run dev
```

Open [the local console](http://127.0.0.1:3000). To check the frontend, run `npm test`, `npm run lint`, and `npm run build`. The local server binds to loopback. A shared deployment requires its own user access controls.
