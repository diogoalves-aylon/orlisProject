# Documentação de Hardware

## 1. Visão Geral
O sistema utiliza uma arquitetura centralizada onde o PLC recolhe todos os dados de campo (sensores e energia) e disponibiliza-os ao servidor web via protocolo Modbus TCP/IP.

**Topologia:**
`Sensores & Medidor` ➡️ `PLC (Concentrador)` ➡️ `Servidor Web (Python/Django)`

---

## 2. Controlador (PLC)
O cérebro da automação local é um controlador programável da gama Free Advance.

* **Fabricante:** Eliwell (Schneider Electric)
* **Modelo:** **AVD6200 C/L/U**
* **Função:** Leitura de sondas de temperatura, transdutores de pressão e gestão da lógica local do chiller.
* **Software de Configuração:** Free Studio Plus.
* **Comunicação:** Porta Ethernet (Modbus TCP) para envio de dados ao servidor.

---

## 3. Medidor de Energia
Para a monitorização elétrica e cálculo de custos, utiliza-se um analisador de energia conectado ao PLC via rede RS485.

* **Fabricante:** Eliwell / Schneider Electric
* **Modelo:** **IEM3255** (Série iEM3000)
* **Função:** Medição de Tensão, Corrente, Potência Ativa (kW) e Energia Total (kWh).
* **Ligação:** Modbus RTU ligado diretamente ao PLC AVD6200.

---

## 4. Sensores de Campo
* **Temperaturas:** Sondas NTC (ligadas às entradas analógicas do PLC).
* **Pressões:** Transdutores 4-20mA (Alta e Baixa pressão).

---
**Data:** Dezembro 2025