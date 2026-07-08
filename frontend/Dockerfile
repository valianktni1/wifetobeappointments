# ---- build stage ----
FROM node:20-alpine AS build
WORKDIR /app

# Relative API path by default: the nginx in this image proxies /api to the backend,
# so REACT_APP_BACKEND_URL is left empty -> the app calls "/api" on the same origin.
ARG REACT_APP_BACKEND_URL=""
ENV REACT_APP_BACKEND_URL=${REACT_APP_BACKEND_URL}
ENV ENABLE_HEALTH_CHECK=false

COPY package.json yarn.lock* ./
RUN yarn install --network-timeout 600000

COPY . .
RUN yarn build

# ---- serve stage ----
FROM nginx:alpine
COPY --from=build /app/build /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
