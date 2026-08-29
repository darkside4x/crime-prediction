FROM node:22-slim AS build
WORKDIR /web
RUN corepack enable
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
RUN pnpm build

FROM nginx:1.27-alpine
COPY --from=build /web/dist /usr/share/nginx/html
COPY <<'EOF' /etc/nginx/conf.d/default.conf
server {
  listen 80;
  root /usr/share/nginx/html;
  location /v1/ { proxy_pass http://api:8000; }
  location /health { proxy_pass http://api:8000; }
  location / { try_files $uri /index.html; }
}
EOF
