FROM node:22-slim AS build
WORKDIR /web
RUN corepack enable
COPY src/web/package.json src/web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY src/web/ .
ARG VITE_API_BASE
ARG VITE_COGNITO_DOMAIN
ARG VITE_COGNITO_CLIENT_ID
ARG VITE_COGNITO_REDIRECT_URI
ARG VITE_COGNITO_LOGOUT_URI
ENV VITE_API_BASE=$VITE_API_BASE \
    VITE_COGNITO_DOMAIN=$VITE_COGNITO_DOMAIN \
    VITE_COGNITO_CLIENT_ID=$VITE_COGNITO_CLIENT_ID \
    VITE_COGNITO_REDIRECT_URI=$VITE_COGNITO_REDIRECT_URI \
    VITE_COGNITO_LOGOUT_URI=$VITE_COGNITO_LOGOUT_URI
RUN pnpm build

FROM nginx:1.31.4-alpine3.24
RUN apk upgrade --no-cache 'libcrypto3>=3.5.8-r0' 'libssl3>=3.5.8-r0'
COPY --from=build /web/dist /usr/share/nginx/html
COPY docker/nginx.conf /etc/nginx/nginx.conf
USER 101:101
EXPOSE 8080
