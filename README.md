# Capri para Windows

Abra Capri.exe com dois cliques. O executável é autocontido e inclui a interface, Python, Pillow, suporte a HEIC/HEIF, AVIF e OpenCV. Não precisa instalar bibliotecas nem iniciar o arquivo BAT.

**Biblioteca e álbuns:** ao importar fotos, Capri procura todas as subpastas da pasta escolhida. As fotos sem álbum ficam juntas em `<pasta escolhida nas preferências>\Biblioteca`. Quando você cria ou atribui fotos a um álbum, os arquivos são movidos para a pasta do álbum dentro do mesmo diretório. A organização acontece no próprio computador.

**Duplicatas visuais:** a busca compara o conteúdo das imagens, independente dos nomes. O app agrupa fotos com similaridade visual acima de 95%, mostra as prévias e escolhe a cópia de maior resolução para manter. Nenhuma duplicata é removida sem sua confirmação; as escolhidas vão para a Lixeira do Windows.

**Interface:** na primeira abertura, Capri pergunta onde guardar os álbuns e a biblioteca. Em Aparência, escolha os temas claro, escuro ou Capri, ajuste cores, fundo, escala, largura da barra lateral, densidade da galeria e arredondamento das miniaturas. A imagem de fundo preserva o enquadramento completo e pode ser reposicionada arrastando a prévia. A divisória entre a barra e a galeria também pode ser arrastada; capas de álbuns na barra são quadradas, sem borda, e mostram o nome ao passar o mouse. Arraste uma foto até um álbum para movê-la; a miniatura acompanha o cursor. Pelo botão direito, escolha "Mover para álbum" e depois selecione o destino, ou abra, devolva à Biblioteca ou exclua a foto. Os avisos rotineiros aparecem dentro da janela sem sons.

**Catálogo e reconhecimento:** a lista de fotos e álbuns permanece em `%LOCALAPPDATA%\Galeria\galeria.db`. A análise facial é local; na primeira utilização, os modelos podem ser baixados depois que você iniciar a busca.
