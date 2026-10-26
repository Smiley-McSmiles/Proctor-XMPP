# sonder.guru landing page

`index.html` is the public landing page for `sonder.guru`. It is a single,
self-contained file (inline CSS and icons) with no external JavaScript or font
dependencies.

Install it as the default site:

```sh
sudo install -d /var/www/html
sudo install -m 0644 index.html /var/www/html/index.html
```

`nginx-sonder.guru.conf` is a complete example vhost for a host where Proctor
also manages Prosody. It:

- serves the landing page from `/var/www/html`;
- keeps the ACME challenge directory available on port 80;
- redirects normal HTTP requests to HTTPS;
- proxies Proctor/Prosody's registration, BOSH, WebSocket, upload, invite page
  and host-meta paths to `127.0.0.1:5280`.

If Proctor already generated its own `proctor-xmpp.conf` for these domains,
merge the `location` blocks and the `/var/www/html` root into that file instead
of enabling two server blocks with the same `server_name`. Then validate and
reload:

```sh
sudo nginx -t
sudo systemctl reload nginx
```

Replace the certificate paths if your certificate is named differently.
