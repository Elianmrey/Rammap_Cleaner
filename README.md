# RAMMAP CLEANER

Aplicativo experimental para Windows que monitora a memória física e solicita, sob demanda, a redução dos Working Sets dos processos e do cache de arquivos do sistema.

> **Importante:** o RAMMAP CLEANER não aumenta a capacidade física de RAM e não garante ganho de desempenho. A remoção de páginas dos Working Sets/cache pode fazer aplicações recarregarem dados, gerar falhas de página e, em alguns cenários, piorar o desempenho. Use com cautela e compare o comportamento do computador antes e depois.

## 1. Recursos

- Interface gráfica em Tkinter, com indicador de RAM em uso, memória disponível, memória total e gráfico histórico.
- Botão manual **“Liberar Momeoria RAM”** (texto mantido conforme solicitado no projeto).
- Opção para ativar/desativar a limpeza automática por pressão de memória.
- Atualização visual a cada 1 segundo.
- Avaliação das regras automáticas a cada 30 segundos.
- Operações de memória executadas em uma thread separada para manter a interface responsiva.
- Registro de eventos, resultados e erros em arquivo de log.
- Solicitação de elevação pelo UAC quando o aplicativo não estiver em execução como Administrador.
- Uso de APIs documentadas do Windows para as operações de Working Set e cache de arquivos.

## 2. Política de monitoramento automático

A limpeza automática fica ativada por padrão. Ela só é acionada quando a memória disponível permanece abaixo do limite crítico pelo tempo configurado, respeitando a histerese e o intervalo de segurança.

| Parâmetro | Valor | Comportamento |
|---|---:|---|
| Atualização visual | 1 segundo | Atualiza os indicadores e o gráfico. |
| Avaliação automática | 30 segundos | Verifica se as condições de pressão foram atendidas. |
| Limite crítico | Menos de 10% disponível | A contagem de pressão começa/continua. O limite é estrito: exatamente 10% não inicia a contagem. |
| Persistência mínima | 60 segundos acumulados | A pressão precisa persistir por esse período antes de acionar um ciclo. |
| Faixa de histerese | De 10% até 15% | Pausa a contagem acumulada; não a zera. |
| Recuperação | Acima de 15% disponível | Reinicia o episódio de pressão e permite um novo episódio no futuro. |
| Intervalo mínimo entre ciclos | 10 minutos | Aplica-se às limpezas automáticas e manuais. |
| Ciclos automáticos por episódio | 1 | Não repete a limpeza automática até a memória superar 15% e um novo episódio começar. |

O intervalo de 10 minutos é um bloqueio de segurança, não uma recomendação de que a limpeza deva ocorrer a cada 10 minutos. Se a memória estiver normal, nenhuma limpeza automática é executada.

### Botão manual

Ao clicar em **“Liberar Momeoria RAM”**, o programa:

1. Verifica se já existe uma limpeza em andamento.
2. Impede uma nova operação enquanto o intervalo mínimo de 10 minutos estiver ativo.
3. Solicita confirmação, explicando os possíveis efeitos da operação.
4. Executa um único ciclo e registra a operação no intervalo de segurança.

A limpeza manual não contorna o intervalo de segurança.

## 3. O que o ciclo de limpeza faz

### 3.1. Working Sets dos processos

O programa enumera os processos com `psutil` e tenta abrir cada processo com a permissão `PROCESS_SET_QUOTA`. Em seguida, chama `SetProcessWorkingSetSize` com os dois parâmetros definidos como `(SIZE_T)-1`, solicitando ao Windows que remova o máximo possível de páginas do Working Set daquele processo.

Por segurança, o programa ignora o próprio processo e os PIDs reservados 0 e 4. Processos protegidos, encerrados durante a operação ou sem acesso permitido podem ser ignorados ou contabilizados como falha.

Uma chamada bem-sucedida não informa quantos megabytes foram efetivamente recuperados. O RAMMAP CLEANER contabiliza os resultados das chamadas, mas não apresenta essa contagem como RAM liberada.

### 3.2. Cache de arquivos do sistema

O programa chama `SetSystemFileCacheSize` com os limites mínimo e máximo definidos como `(SIZE_T)-1` e `Flags=0`, solicitando a liberação do cache de arquivos do sistema.

Antes da chamada, tenta habilitar o privilégio `SeIncreaseQuotaPrivilege` no token do processo. Mesmo com elevação administrativa, o privilégio pode não estar disponível no token ou a chamada pode falhar; nesse caso, o erro é registrado e a aplicação continua funcionando.

Essa operação atua no cache de arquivos do sistema. Ela não equivale a esvaziar toda a memória do kernel, encerrar processos, limpar toda a memória virtual ou esvaziar diretamente a Standby List.

## 4. Requisitos para executar pelo código-fonte

- Windows 10 ou Windows 11.
- Python 3.10 ou superior (recomendado Python de 64 bits em Windows de 64 bits).
- `tkinter`, normalmente incluído na instalação oficial do Python para Windows.
- Dependência de execução: `psutil`.
- Privilégios de Administrador para executar as operações de memória.

### Instalação e execução

Abra PowerShell na pasta do projeto:

```powershell
py -m pip install -r requirements.txt
py rammap_cleaner_gui.py
```

O aplicativo solicitará elevação pelo UAC quando necessário. Aceite a solicitação para continuar.

## 5. Testes automatizados

Os testes validam a política de pressão de memória, histerese, intervalo de segurança, rearmamento de episódios, cálculo dos indicadores, tratamento de erros e fluxo do executor.

Para instalar o pytest e executar os testes:

```powershell
py -m pip install pytest
py -m pytest -v
```

**Escopo dos testes:** os testes usam relógio controlado e APIs simuladas. Eles não fazem chamadas reais de limpeza de memória, não exigem elevação administrativa e podem ser executados fora do Windows. Portanto, passar nos testes valida a lógica Python, mas não substitui a validação das APIs reais em um computador Windows.

A suíte incluída possui 19 testes. O resultado mais recente desta versão foi **19 aprovados** no ambiente de desenvolvimento utilizado para revisar o pacote.

## 6. Executar testes e iniciar o programa pelo launcher

Para executar a sequência de verificação antes de abrir a interface:

1. Extraia a pasta completa do projeto.
2. Dê duplo clique em `Executar_RAMMAP_CLEANER.bat`.
3. Autorize a elevação pelo UAC.
4. Aguarde a verificação das dependências e a execução de `pytest`.
5. A interface só será iniciada se todos os testes terminarem com sucesso.

O arquivo `Executar_RAMMAP_CLEANER.ps1` contém a lógica do launcher. Ele solicita elevação, localiza Python 3.10 ou superior, verifica `psutil`, `pytest` e `tkinter`, tenta instalar `psutil` e `pytest` se estiverem ausentes e interrompe o fluxo caso qualquer teste falhe.

A instalação automática das dependências pelo launcher pode exigir conexão com a internet. O launcher usa o interpretador Python selecionado no computador e instala as dependências nesse ambiente; para builds reproduzíveis, prefira o ambiente virtual descrito na seção seguinte.

## 7. Gerar um único executável `.exe`

O projeto pode ser empacotado com PyInstaller. O resultado é um executável único que inclui o interpretador Python e as dependências necessárias; o usuário final não precisa instalar Python ou `psutil` para executar o arquivo empacotado.

> **A compilação do executável Windows deve ser feita no próprio Windows.** O PyInstaller não é um compilador cruzado: para gerar um executável Windows, execute o processo de build em Windows.

### 7.1. Preparar o ambiente de build

Abra PowerShell na pasta `rammap_cleaner_project` e execute:

```powershell
py -3 -m venv .venv

.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pytest pyinstaller
```

Se o comando `py -3` não estiver disponível, instale Python 3.10 ou superior e use o launcher `py` fornecido pelo instalador oficial do Python.

### 7.2. Executar os testes antes de compilar

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

**Não prossiga com a compilação se algum teste falhar.** Corrija a causa, execute os testes novamente e só compile quando todos passarem.

### 7.3. Compilar o executável único

```powershell
.\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --uac-admin `
    --name RAMMAP_CLEANER `
    rammap_cleaner_gui.py
```

O executável será criado em:

```text
dist\RAMMAP_CLEANER.exe
```

### 7.4. Significado das opções

| Opção | Finalidade |
|---|---|
| `--onefile` | Empacota o aplicativo em um único arquivo executável. |
| `--windowed` | Não abre uma janela de console junto à interface gráfica. |
| `--uac-admin` | Inclui um manifesto que solicita elevação administrativa na inicialização. |
| `--clean` | Limpa cache e arquivos temporários do PyInstaller antes do build. |
| `--noconfirm` | Substitui artefatos de build existentes sem solicitar confirmação. |
| `--name RAMMAP_CLEANER` | Define o nome do executável. |

O próprio código também possui uma rotina de solicitação de UAC. Com o manifesto `--uac-admin`, o Windows solicita a elevação antes de iniciar o aplicativo; a verificação interna identifica que o processo já está elevado e não deve solicitar uma segunda elevação.

### 7.5. Distribuir e executar

Para distribuição simples, o arquivo a entregar ao usuário final é:

```text
dist\RAMMAP_CLEANER.exe
```

O usuário final pode dar duplo clique no executável e aceitar o aviso do UAC. Não precisa ter Python, `psutil`, pytest ou PyInstaller instalados.

O modo `--onefile` produz um único arquivo para distribuição, mas o bootloader do PyInstaller extrai componentes internos para uma pasta temporária durante a execução. Isso é normal. O primeiro início pode demorar mais do que os seguintes.

Os testes e arquivos `.py` não são necessários para executar a versão compilada. Mantenha-os no repositório de desenvolvimento para permitir auditoria, correções e novas compilações.

## 8. Logs

O arquivo de log é criado em:

```text
%LOCALAPPDATA%\RAMMapCleaner\rammap_cleaner.log
```

Para abrir a pasta no Windows, pressione `Win + R`, cole `%LOCALAPPDATA%\RAMMapCleaner` e pressione Enter.

O log registra inicialização, ciclos manuais e automáticos, resultados por processo, falhas de API e erros de monitoramento.

## 9. Limitações e recomendações

- O programa não adiciona RAM, não altera a capacidade física instalada e não garante aumento de FPS ou velocidade.
- Uma redução no Working Set não significa que os dados foram apagados: páginas podem voltar a ser carregadas quando os aplicativos precisarem delas.
- O sistema pode apresentar mais page faults e atividade de disco depois de uma limpeza.
- O Windows já gerencia Working Sets, memória disponível e cache. Não use a limpeza como tarefa periódica agressiva nem mantenha ciclos de poucos segundos.
- O percentual de memória disponível é um indicador de pressão, não uma medição direta da quantidade que a operação vai recuperar.
- Uma chamada de API bem-sucedida não garante que uma quantidade específica de RAM ficará disponível.
- Antivírus ou SmartScreen podem exibir avisos para executáveis PyInstaller não assinados. Distribua somente builds produzidos a partir de código confiável; para distribuição pública, considere assinatura digital do executável.
- Teste o `.exe` em versões de Windows e configurações de segurança compatíveis antes de distribuí-lo a terceiros.

## 10. Estrutura do projeto

```text
rammap_cleaner_project/
├── rammap_cleaner_gui.py          # Aplicativo e interface gráfica
├── test_rammap_cleaner.py         # Testes automatizados
├── requirements.txt               # Dependências de execução
├── Executar_RAMMAP_CLEANER.bat    # Atalho para o launcher PowerShell
├── Executar_RAMMAP_CLEANER.ps1    # Eleva, testa e inicia o programa
└── README.md                      # Esta documentação
```

Durante a compilação, o PyInstaller também cria `build/`, `dist/` e `RAMMAP_CLEANER.spec`. O arquivo final para distribuição fica em `dist/`.

## 11. Referências técnicas

- [Microsoft Learn — SetProcessWorkingSetSize](https://learn.microsoft.com/pt-br/windows/win32/api/memoryapi/nf-memoryapi-setprocessworkingsetsize)
- [Microsoft Learn — SetSystemFileCacheSize](https://learn.microsoft.com/pt-br/windows/win32/api/memoryapi/nf-memoryapi-setsystemfilecachesize)
- [Microsoft Learn — Process Working Set](https://learn.microsoft.com/pt-br/windows/win32/procthread/process-working-set)
- [PyInstaller — Manual](https://www.pyinstaller.org/en/stable/)
- [PyInstaller — Opções de linha de comando](https://pyinstaller.org/en/stable/usage.html)

---

**RAMMAP CLEANER — projeto experimental e educacional.** Use com cautela e nunca interprete uma redução no indicador de memória em uso, isoladamente, como prova de melhoria de desempenho.
