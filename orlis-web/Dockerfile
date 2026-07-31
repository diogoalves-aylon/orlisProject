# --- Etapa 1: build dos assets do frontend ---------------------------------------
#
# O tema vive em src/assets/vendor (CSS do Bootstrap, fontes, ícones, libs JS) e é gerado
# por gulp — está no .gitignore, portanto NÃO vem no clone. Sem esta etapa, o collectstatic
# recolhe só os assets versionados, todo o /static/vendor/... dá 404 e a interface aparece
# sem CSS. O gulp escreve em ./assets/vendor (ver src/build-config.js: distPath).
#
# Node 20 e não 22: o gulp 4.0.2 desta toolchain é sensível a versões mais recentes. Usa
# dart-sass (gulp-dart-sass), por isso não há compilação nativa envolvida.
FROM node:20 AS frontend

WORKDIR /build/src

# Manifesto e lockfile primeiro: o cache desta camada só é invalidado quando as
# dependências mudam, não a cada alteração de código.
COPY src/package.json src/yarn.lock ./

# Usa-se o yarn que a imagem node:20 já traz, que é o 1.22 clássico — e é o que este
# projeto precisa: o src/yarn.lock é "yarn lockfile v1" e o package.json não tem campo
# packageManager, portanto um `corepack enable` resolveria para o Yarn 4, que não lê este
# formato nem aceita --frozen-lockfile (usa --immutable). Instalar o yarn com
# `npm install -g` também não serve: falha com EEXIST porque /usr/local/bin/yarn já existe.
# O `yarn --version` fica no log do build para a versão usada ser visível.
RUN yarn --version && yarn install --frozen-lockfile --network-timeout 600000

COPY src/ ./
RUN yarn build:prod


# --- Etapa 2: aplicação -----------------------------------------------------------
# Usa a imagem oficial do Python 3.12
FROM python:3.12

# Define o diretório da aplicação
ARG APP_HOME=/app
WORKDIR ${APP_HOME}

# Evita que o Python crie arquivos .pyc
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Instala dependências do sistema operacional (inclui o cliente do PostgreSQL)
RUN apt update && apt install -y postgresql-client

# Copia as dependências primeiro para otimizar cache no build
COPY requirements.txt ${APP_HOME}/requirements.txt

# Instala as dependências do Python
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código da aplicação
COPY . ${APP_HOME}

# Os assets construídos vêm DEPOIS do código, senão o COPY acima sobrepunha-se a eles
# quando o build corre numa máquina que já tem o vendor/ gerado localmente.
COPY --from=frontend /build/src/assets/vendor ${APP_HOME}/src/assets/vendor

# Coleta os arquivos estáticos antes de iniciar o servidor
CMD ["sh", "-c", "python manage.py migrate && python manage.py collectstatic --noinput && gunicorn --config gunicorn-cfg.py config.wsgi"]
