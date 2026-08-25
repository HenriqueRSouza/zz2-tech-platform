# Deploy na VPS (Docker + nginx)

Este procedimento foi preparado para uma VPS Ubuntu e para o repositório
`HenriqueRSouza/zz2-tech-platform`. O container publica a aplicação somente em
`127.0.0.1:8080`; nginx é a única entrada pública e termina o HTTPS.

## 1. DNS e dependências

Antes de começar, crie um registro DNS `A` apontando o domínio da aplicação para
o IP público da VPS. Depois, conecte-se por SSH. No Ubuntu 22.04/24.04, instale o
Docker pelo repositório oficial e os demais pacotes assim:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git nginx certbot

sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker nginx
```

Ao terminar, estes comandos precisam funcionar:

```bash
git --version
docker --version
docker compose version
nginx -v
certbot --version
```

Libere no firewall somente SSH, HTTP e HTTPS (portas 22, 80 e 443). A porta 8080
não deve ser liberada publicamente.

## 2. Clonar e configurar

```bash
sudo git clone https://github.com/HenriqueRSouza/zz2-tech-platform.git /opt/zz2
cd /opt/zz2
sudo cp deploy/.env.example deploy/.env
sudo chmod 600 deploy/.env
sudo nano deploy/.env
```

No mínimo, substitua o domínio e todas as ocorrências de `REPLACE` ou
`YOUR-PROJECT`. Se a integração com Google Apps Script não for usada, defina
`ZZ2_ENABLE_APPSCRIPT_SYNC=false` e deixe URL/segredo vazios.

Valide e inicie:

```bash
sudo ./deploy/check_env.sh
sudo ./deploy/start_docker.sh
```

## 3. Ativar o HTTPS

Troque domínio e e-mail pelos valores reais. O DNS já deve estar propagado e as
portas 80/443 precisam estar acessíveis.

```bash
sudo ./deploy/configure_nginx.sh suporte.seudominio.com.br voce@seudominio.com.br
ZZ2_URL=https://suporte.seudominio.com.br ./deploy/smoke_test.sh
sudo certbot renew --dry-run
```

## 4. Atualizar depois de enviar mudanças ao GitHub

```bash
ssh usuario@IP_DA_VPS
cd /opt/zz2
sudo ./deploy/update_docker.sh
```

O script aceita somente `fast-forward`, reconstrói o container, espera o novo
build ficar saudável e volta automaticamente ao commit anterior se a aplicação
não subir. O volume Docker `zz2-data` não é alterado.

## Diagnóstico rápido

```bash
cd /opt/zz2
sudo docker compose -f deploy/docker-compose.yml ps
sudo docker compose -f deploy/docker-compose.yml logs --tail=200 app
curl -fsS http://127.0.0.1:8080/healthz
sudo nginx -t
sudo journalctl -u nginx -n 100 --no-pager
```

Faça backup periódico do volume `zz2-data` e do arquivo `deploy/.env`. O `.env`
contém segredos e é ignorado pelo Git; nunca o envie ao GitHub.
