FROM node:22-slim AS build
WORKDIR /web
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build

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
