# Controle de Estoque Mercado Livre — Gutos Car

Primeira versão do sistema separado para controlar estoque operacional do Mercado Livre.

## Funções

- Cadastro manual de produtos reais do estoque ML.
- Cadastro/importação de anúncios do Mercado Livre.
- Composição de anúncio/kit: um anúncio pode baixar um ou mais produtos reais.
- Importação de relatório de vendas do Mercado Livre.
- Baixa automática de estoque para vendas que **não** são Full.
- Vendas com `Forma de entrega = Mercado Envios Full` são registradas, mas **não baixam estoque**, pois a baixa deve ocorrer quando a mercadoria é enviada ao Full.
- Histórico de movimentações.
- Backup Excel das principais tabelas.

## Variáveis de ambiente

No Render, configure:

```text
DATABASE_URL=<string de conexão PostgreSQL/Neon>
SECRET_KEY=<qualquer texto forte>
```

## Rodar localmente

```bash
pip install -r requirements.txt
python app.py
```

## Deploy Render

Build Command:

```bash
pip install -r requirements.txt
```

Start Command:

```bash
gunicorn app:app
```

## Modelo da planilha de produtos/anúncios

Baixe pelo menu **Importar produtos > Baixar modelo Excel**.

Colunas principais:

```text
CODIGO_PRODUTO
CODIGOS_ALTERNATIVOS
SKU_PRODUTO
DESCRICAO_PRODUTO
QUANTIDADE_ESTOQUE
ESTOQUE_MINIMO
LOCALIZACAO
CUSTO_PRODUTO
OBSERVACOES
CODIGO_ANUNCIO_ML
SKU_ANUNCIO
TITULO_ANUNCIO
FORMA_ENTREGA
STATUS_ANUNCIO
QTD_POR_VENDA
```

Para kit, coloque uma linha por componente com o mesmo `CODIGO_ANUNCIO_ML` e a quantidade do componente em `QTD_POR_VENDA`.

Exemplo:

```text
MLB123 | AMORT-GOL | QTD_POR_VENDA 2
MLB123 | KIT-COXIM-GOL | QTD_POR_VENDA 2
```

Quando vender 1 unidade do anúncio MLB123, o sistema baixa 2 amortecedores e 2 kits de coxim.
