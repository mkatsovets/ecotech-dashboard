# Deploying to V/AM (custom Docker container)

The app is one container: FastAPI + static page, SQLite on a persistent volume.
Image: built by GitHub Actions on every push to `main`, published to Docker Hub.

## 1. One-time: put the code on GitHub and get an image on Docker Hub

1. Docker Hub: create an access token (Account settings -> Security -> New access token,
   scope Read & Write). Nothing else is needed there: the repository is created by the first push.
2. Create an empty **private** repository on GitHub (for example `ecotech-dashboard`), no README.
3. In it: Settings -> Secrets and variables -> Actions -> add `DOCKERHUB_USERNAME` (your Docker Hub login)
   and `DOCKERHUB_TOKEN` (the token from step 1).
4. Push this project:
   ```powershell
   git remote add origin https://github.com/<owner>/ecotech-dashboard.git
   git push -u origin main
   ```
5. The `docker` workflow (Actions tab) runs the tests and publishes
   `docker.io/<DOCKERHUB_USERNAME>/ecotech-dashboard:latest`.
6. On Docker Hub make the repository **public** (a free account has one private repository;
   if you keep it private, add the registry credentials in V/AM). The image holds only code,
   configuration and the cooperative's report data, no secrets.

## 2. Create the environment in V/AM

1. **New environment -> Docker** tab -> **Select image** -> Docker Hub,
   image `<DOCKERHUB_USERNAME>/ecotech-dashboard`, tag `latest`.
2. **Volumes:** mount a volume on `/data` (this is where `ecotech.db` is stored; without it the
   database is lost on every redeploy).
3. **Variables:** none required (`PORT=80` is the default in the image). Later, for the admin side:
   `ADMIN_PASSWORD`, `ANTHROPIC_API_KEY`. Never put them in the image or the repository.
4. **Resources:** 1 node, ~256 MB RAM / 1 cloudlet range is enough.
5. **Public access:** the container listens on port 80, which is where the platform's shared load
   balancer sends HTTP, so the environment URL works as is. For a custom domain / HTTPS use the
   Let's Encrypt add-on or an NGINX load balancer layer in front.
6. Open the environment URL: the page must load and `/healthz` must return `{"status":"ok"}`.

### Troubleshooting

- *502 "The server is not listening to the :80 port on its backend"*: the app listens on another
  port. Make sure the container variable `PORT` is `80` (older images had `8080`; edit the variable
  and restart the container) and look at the container log.
- *"The image is based on unsupported OS template for deploying it as a custom container"*:
  the base image must be built on an OS version the platform supports. The Dockerfile pins
  `python:3.12-slim-bookworm` (Debian 12) for this reason. If it is still refused, try
  `python:3.12-alpine` or deploy the same image on a **Docker Engine CE** node
  (the alternative the platform itself suggests).

## 3. Updating

Push to `main` -> wait for the green `docker` workflow -> in V/AM press **Redeploy** on the
container (tag `latest`). The volume with the database is kept.

## 4. Backup

Back up the `/data` volume (V/AM backup) or copy `/data/ecotech.db` periodically.
The database holds the FX cache and, once the admin side exists, every report entered through it.

## Known limits

- SQLite means exactly one running instance (do not scale horizontally).
- Until the admin side is built, reports come only from `data/seed/eti1.yaml` (inside the image),
  which is re-applied on each start.
- The page loads Plotly from the jsDelivr CDN; the browsers of the viewers need internet access.
