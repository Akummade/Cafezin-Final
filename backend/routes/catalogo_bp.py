from datetime import datetime

from sqlalchemy import func
from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from permissoes import tipo_requerido
from models.comprador import Comprador
from models.comprador_endereco import Comprador_Endereco
from models.produto import Produto
from models.loja import Loja
from models.usuario import Usuario
from models.pagamento import Pagamento
from models.pedido import Pedido
from models.categoria import Categoria
from models.item_pedido import ItemPedido
from models.produto_popularidade import ProdutoPopularidade
from models.avaliacao_produto import AvaliacaoProduto
from models.avaliacao_loja import AvaliacaoLoja
from models.loja_popularidade import LojaPopularidade


catalogo_bp = Blueprint("catalogo", __name__)

VALOR_FRETE_PADRAO = 5.00
FRETE_GRATIS_A_PARTIR_DE = 25.00   # mesma regra do front (sacolaDados.js)
LIMITE_QUANTIDADE_ITEM = 20
FORMAS_PAGAMENTO = ["PIX", "Boleto", "Cartão", "Dinheiro", "Transferência"]


@catalogo_bp.errorhandler(SQLAlchemyError)
def erro_banco(erro):
    """Qualquer erro do SQLAlchemy (conexão, query, etc.) cai aqui."""
    db.session.rollback()
    return {"error": f"erro ao acessar o banco de dados: {erro}"}, 500


@catalogo_bp.route("/produtos", methods=["GET"])
def catalogo():
    cod_categoria = request.args.get("categoria", type=int)

    query = (
        Produto.query.join(Produto.loja)
        .join(Loja.usuario)
        .filter(Produto.disponibilidade.is_(True))
    )

    if cod_categoria:
        query = query.filter(Produto.cod_categoria == cod_categoria)

    produtos = query.order_by(Usuario.nome, Produto.nome).all()

    return {
        "produtos": [
            {
                "cod_produto": p.cod_produto,
                "nome": p.nome,
                "preco": float(p.preco),
                "loja": p.loja.usuario.nome,
                "cod_loja": p.cod_loja,
                "cod_categoria": p.cod_categoria,
                "foto_url": p.foto_url,
            }
            for p in produtos
        ]
    }


@catalogo_bp.route("/lojas", methods=["GET"])
def catalogo_lojas():

    lojas = ( db.session.query (
            Loja,
            Usuario.nome,
            func.avg(AvaliacaoLoja.nota).label("nota_media"),
            func.count(AvaliacaoLoja.cod_avaliacao_loja).label("total_avaliacoes"),
        ).join(Usuario, Loja.cod_usuario == Usuario.cod_usuario)
        .outerjoin(Pedido, Pedido.cod_loja == Loja.cod_loja)
        .outerjoin(AvaliacaoLoja, Pedido.cod_pedido == AvaliacaoLoja.cod_pedido)
        .group_by(Loja.cod_loja, Usuario.nome)
        .all()
    )


    mostrar_lojas = []
    for loja, nome, nota_media, total_avaliacoes in lojas:
        loja_map = {
            "cod_loja": loja.cod_loja,
            "nome": nome,
            "nota_media": round(float(nota_media or 0), 1),
            "total_avaliacoes": total_avaliacoes,
        }

        mostrar_lojas.append(loja_map)

    return {"lojas": mostrar_lojas}


@catalogo_bp.route("/produtos/<int:cod_produto>", methods=["GET"])
def produto_detalhe(cod_produto):
    produto = Produto.query.get_or_404(cod_produto)

    notas = [a.nota for a in produto.avaliacoes if a.nota is not None]
    media_avaliacao = round(sum(notas) / len(notas), 1) if notas else None

    return {
        "cod_produto": produto.cod_produto,
        "nome": produto.nome,
        "descricao": produto.descricao,
        "preco": float(produto.preco),
        "loja": produto.loja.usuario.nome,
        "media_avaliacao": media_avaliacao,
        "total_avaliacoes": len(notas),
        "foto_url" : produto.foto_url,
        "disponibilidade" : produto.disponibilidade,
        "cod_loja" : produto.loja.cod_loja,
        "cod_categoria" : produto.cod_categoria
    }


@catalogo_bp.route("/pedidos", methods=["POST"])
@tipo_requerido("Comprador")
def criar_pedido():
    """Cria um pedido com vários itens da MESMA loja (a sacola do front).

    Corpo: {"itens": [{"cod_produto": 1, "quantidade": 2}, ...],
            "cod_endereco": 7, "forma_pagamento": "PIX"}
    O comprador vem do token e os preços/frete são calculados aqui, no servidor.
    (O formato antigo, com um único cod_produto, continua aceito.)
    """
    dados = request.get_json(silent=True) or {}

    comprador = Comprador.query.filter_by(cod_usuario=get_jwt_identity()).first()
    if comprador is None:
        return {"error": "comprador não encontrado"}, 404

    forma_pagamento = dados.get("forma_pagamento")
    if forma_pagamento not in FORMAS_PAGAMENTO:
        return {"error": "forma de pagamento inválida"}, 400

    # o endereço precisa ser um dos endereços do próprio comprador
    cod_endereco = dados.get("cod_endereco")
    vinculo = Comprador_Endereco.query.filter_by(
        cod_comprador=comprador.cod_comprador, cod_endereco=cod_endereco
    ).first()
    if vinculo is None:
        return {"error": "endereço de entrega inválido"}, 400

    itens_pedido = dados.get("itens")
    if itens_pedido is None and dados.get("cod_produto") is not None:
        itens_pedido = [{
            "cod_produto": dados["cod_produto"],
            "quantidade": dados.get("quantidade", 1),
        }]
    if not isinstance(itens_pedido, list) or not itens_pedido:
        return {"error": "o pedido precisa ter ao menos um item"}, 400

    # junta repetidos e valida as quantidades
    quantidades = {}
    for item in itens_pedido:
        try:
            cod_produto = int(item["cod_produto"])
            quantidade = int(item.get("quantidade", 1))
        except (KeyError, TypeError, ValueError, AttributeError):
            return {"error": "item inválido no pedido"}, 400
        quantidades[cod_produto] = quantidades.get(cod_produto, 0) + quantidade

    if any(q < 1 or q > LIMITE_QUANTIDADE_ITEM for q in quantidades.values()):
        return {"error": f"a quantidade de cada produto deve ficar entre 1 e {LIMITE_QUANTIDADE_ITEM}"}, 400

    produtos = Produto.query.filter(Produto.cod_produto.in_(list(quantidades))).all()
    if len(produtos) != len(quantidades):
        return {"error": "um ou mais produtos não foram encontrados"}, 404

    indisponiveis = [p.nome for p in produtos if not p.disponibilidade]
    if indisponiveis:
        return {"error": "produto indisponível no momento: " + ", ".join(indisponiveis)}, 409

    if len({p.cod_loja for p in produtos}) != 1:
        return {"error": "todos os produtos do pedido devem ser da mesma loja"}, 400

    subtotal = round(sum(float(p.preco) * quantidades[p.cod_produto] for p in produtos), 2)
    valor_frete = 0.0 if subtotal >= FRETE_GRATIS_A_PARTIR_DE else VALOR_FRETE_PADRAO
    valor_total = round(subtotal + valor_frete, 2)
    agora = datetime.now()

    # cria o Pagamento e usa flush() para já ter o cod_pagamento gerado,
    # sem precisar de um commit isolado por tabela
    pagamento = Pagamento(tipo=forma_pagamento, data=agora)
    db.session.add(pagamento)
    db.session.flush()

    pedido = Pedido(
        data_pedido=agora,
        valor_frete=valor_frete,
        valor_total=valor_total,
        status="Pendente",
        cod_comprador=comprador.cod_comprador,
        cod_loja=produtos[0].cod_loja,
        cod_end_entrega=cod_endereco,
        cod_pagamento=pagamento.cod_pagamento,
    )
    db.session.add(pedido)
    db.session.flush()

    for produto in produtos:
        db.session.add(ItemPedido(
            quantidade=quantidades[produto.cod_produto],
            preco_unitario=produto.preco,
            cod_pedido=pedido.cod_pedido,
            cod_produto=produto.cod_produto,
        ))

    db.session.commit()

    return {
        "message": "pedido criado",
        "cod_pedido": pedido.cod_pedido,
        "valor_frete": valor_frete,
        "valor_total": valor_total,
    }, 201


@catalogo_bp.route("/pedidos/<int:cod_pedido>", methods=["GET"])
def pedido_confirmacao(cod_pedido):
    pedido = Pedido.query.get_or_404(cod_pedido)

    return {
        "cod_pedido": pedido.cod_pedido,
        "status": pedido.status,
        "valor_total": float(pedido.valor_total),
        "valor_frete": float(pedido.valor_frete),
        "itens": [
            {
                "produto": item.produto.nome,
                "quantidade": item.quantidade,
                "preco_unitario": float(item.preco_unitario),
            }
            for item in pedido.itens
        ],
    }


NOTA_MINIMA = 3

@catalogo_bp.route("/produtos/populares", methods=["GET"])
def produtos_populares():
    limite = request.args.get("limite", default=10, type=int)

    resultados = (
        db.session.query(Produto, ProdutoPopularidade.total_vendido)
        .join(ProdutoPopularidade, Produto.cod_produto == ProdutoPopularidade.cod_produto)
        .join(AvaliacaoProduto, Produto.cod_produto == AvaliacaoProduto.cod_produto)
        .filter(Produto.disponibilidade.is_(True))
        .group_by(Produto.cod_produto, ProdutoPopularidade.total_vendido)
        .having(func.avg(AvaliacaoProduto.nota) >= NOTA_MINIMA)
        .order_by(ProdutoPopularidade.total_vendido.desc())
        .limit(limite)
        .all()
    )

    return {
        "produtos": [
            {
                "cod_produto": p.cod_produto,
                "nome": p.nome,
                "preco": float(p.preco),
                "total_vendido": total_vendido,
                "cod_loja": p.cod_loja,
                "nome_loja": p.loja.usuario.nome,
                "foto_url": p.foto_url,
            }
            for p, total_vendido in resultados
        ]
    }


NOTA_MINIMA = 3
LIMITE_PRODUTOS_POR_LOJA = 6

@catalogo_bp.route("/lojas/populares", methods=["GET"])
def lojas_populares():
    limite = request.args.get("limite", default=3, type=int)
    offset = request.args.get("offset", default=0, type=int)
    incluir_produtos = request.args.get("incluir_produtos", default="true") == "true"

    resultados = (
        db.session.query(
            Loja,
            Usuario.nome,
            LojaPopularidade.total_vendido,
            func.avg(AvaliacaoLoja.nota).label("nota_media"),
            func.count(AvaliacaoLoja.cod_avaliacao_loja).label("total_avaliacoes"),
        )
        .join(Usuario, Loja.cod_usuario == Usuario.cod_usuario)
        .join(LojaPopularidade, Loja.cod_loja == LojaPopularidade.cod_loja)
        .join(Pedido, Loja.cod_loja == Pedido.cod_loja)
        .join(AvaliacaoLoja, Pedido.cod_pedido == AvaliacaoLoja.cod_pedido)
        .group_by(Loja.cod_loja, Usuario.nome, LojaPopularidade.total_vendido)
        .having(func.avg(AvaliacaoLoja.nota) >= NOTA_MINIMA)
        .order_by(LojaPopularidade.total_vendido.desc())
        .limit(limite)
        .offset(offset)
        .all()
    )

    lojas_json = []
    for loja, nome, total_vendido, nota_media, total_avaliacoes in resultados:
        loja_dict = {
            "cod_loja": loja.cod_loja,
            "nome": nome,
            "total_vendido": total_vendido,
            "nota_media": round(float(nota_media), 1),
            "total_avaliacoes": total_avaliacoes,
        }

        if incluir_produtos:
            produtos = (
                Produto.query.filter_by(cod_loja=loja.cod_loja, disponibilidade=True)
                .limit(LIMITE_PRODUTOS_POR_LOJA)
                .all()
            )
            loja_dict["produtos"] = [
                {
                    "cod_produto": p.cod_produto,
                    "nome": p.nome,
                    "preco": float(p.preco),
                    "foto_url": p.foto_url,
                }
                for p in produtos
            ]

        lojas_json.append(loja_dict)

    return {"lojas": lojas_json}

@catalogo_bp.route("/categorias/<int:cod_categoria>/lojas", methods=["GET"])
def lojas_da_categoria(cod_categoria):
    categoria = db.session.get(Categoria, cod_categoria)
    if categoria is None:
        return {"error" : "categoria não encontrada"}, 404
    
    linhas = (
        db.session.query(Produto, Loja, Usuario)
        .join(Loja, Loja.cod_loja == Produto.cod_loja)
        .join(Usuario, Usuario.cod_usuario == Loja.cod_usuario)
        .filter(
            Produto.cod_categoria == cod_categoria,
            Produto.disponibilidade.is_(True),
        )
        .order_by(Produto.nome)
        .all()
    )

    lojas = {}

    for produto, loja, usuario in linhas:
        entrada = lojas.setdefault(loja.cod_loja, {
            "cod_loja" : loja.cod_loja,
            "nome" : usuario.nome,
            "foto_url" : usuario.foto_url,
            "produtos" : [],
        })
        entrada["produtos"].append({
            "cod_produto" : produto.cod_produto,
            "nome" : produto.nome,
            "preco" : float(produto.preco),
            "foto_url" : produto.foto_url,
        })

    return {"categoria" : categoria.nome_categoria, "lojas" : list(lojas.values())}

def _loja_aberta(loja):
    if not loja.horario_abertura or not loja.horario_fecha:
        return None
    agora = datetime.now().time()
    if loja.horario_abertura <= loja.horario_fecha:
        return loja.horario_abertura <= agora <= loja.horario_fecha
    return agora >= loja.horario_abertura or agora <= loja.horario_fecha


@catalogo_bp.route("/lojas/<int:cod_loja>/cardapio", methods=["GET"])
def cardapio_da_loja(cod_loja):
    loja = db.session.get(Loja, cod_loja)
    if loja is None:
        return {"error": "loja não encontrada"}, 404

    nota_media, total_avaliacoes = (
        db.session.query(
            func.avg(AvaliacaoLoja.nota),
            func.count(AvaliacaoLoja.cod_avaliacao_loja),
        )
        .join(Pedido, Pedido.cod_pedido == AvaliacaoLoja.cod_pedido)
        .filter(Pedido.cod_loja == cod_loja)
        .one()
    )

    linhas = (
        db.session.query(Produto, Categoria.nome_categoria)
        .outerjoin(Categoria, Categoria.cod_categoria == Produto.cod_categoria)
        .filter(Produto.cod_loja == cod_loja, Produto.disponibilidade.is_(True))
        .order_by(Categoria.nome_categoria, Produto.nome)
        .all()
    )

    return {
        "cod_loja": loja.cod_loja,
        "nome": loja.usuario.nome,
        "foto_url": loja.usuario.foto_url,
        "horario_abertura": loja.horario_abertura.strftime("%H:%M") if loja.horario_abertura else None,
        "horario_fecha": loja.horario_fecha.strftime("%H:%M") if loja.horario_fecha else None,
        "aberta": _loja_aberta(loja),
        "nota_media": round(float(nota_media), 1) if nota_media is not None else None,
        "total_avaliacoes": total_avaliacoes,
        "produtos": [
            {
                "cod_produto": p.cod_produto,
                "nome": p.nome,
                "descricao": p.descricao,
                "preco": float(p.preco),
                "foto_url": p.foto_url,
                "categoria": nome_categoria,
            }
            for p, nome_categoria in linhas
        ],
    }

@catalogo_bp.route("/categorias", methods=["GET"])
def listar_categorias():
    categorias = Categoria.query.order_by(Categoria.nome_categoria).all()

    return {
        "categorias": [
            {
                "cod_categoria": c.cod_categoria,
                "nome": c.nome_categoria,
            }
            for c in categorias
        ]
    }