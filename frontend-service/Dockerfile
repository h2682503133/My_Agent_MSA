FROM nginx:1.27-alpine

COPY login.html /usr/share/nginx/html/login.html
COPY chat.html /usr/share/nginx/html/chat.html
COPY chat.css /usr/share/nginx/html/chat.css
COPY chat.js /usr/share/nginx/html/chat.js
COPY chat-mobile.html /usr/share/nginx/html/chat-mobile.html
COPY chat-mobile.css /usr/share/nginx/html/chat-mobile.css
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
