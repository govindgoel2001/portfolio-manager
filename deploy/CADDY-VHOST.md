# Serving the dashboard from an existing Caddy

The VPS already runs Caddy on 80 and 443 for other sites, so this stack does
not start a second one. The app binds `127.0.0.1:8791` and the host Caddy
proxies to it.

Append this to `/etc/caddy/Caddyfile`, then `systemctl reload caddy`:

```
pm.YOUR-SERVER-IP.sslip.io {
	encode zstd gzip

	reverse_proxy 127.0.0.1:8791

	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options nosniff
		X-Frame-Options DENY
		Referrer-Policy no-referrer
		X-Robots-Tag "noindex, nofollow"
		-Server
	}

	log {
		output file /var/log/caddy/pm-access.log
		format console
	}
}
```

The app authenticates every request itself, including `/api/*`, using
`PM_DASHBOARD_PASSWORD`. That is the gate. If you want a second one in front of
it the way the GEX site does, add a `basic_auth` block here too; the cost is
logging in twice.

Validate before reloading, because a syntax error takes every site on the box
down with it:

```
caddy validate --config /etc/caddy/Caddyfile
```

To change the port, set `PM_PORT` in `.env` and update the `reverse_proxy` line
to match.
