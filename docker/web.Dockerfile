FROM node:22-slim AS build
WORKDIR /web
RUN corepack enable
COPY src/web/package.json src/web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY src/web/ .
RUN pnpm build

FROM nginx:1.31.4-alpine3.24
COPY --from=build /web/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/nginx.conf
USER 101:101
EXPOSE 8080
