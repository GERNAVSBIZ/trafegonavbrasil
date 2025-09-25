# -*- coding: utf-8 -*-

from flask import Flask, render_template, request, jsonify
import pandas as pd
import re
from datetime import datetime
import io
import os
import json
import firebase_admin
from firebase_admin import credentials, auth, firestore

app = Flask(__name__)
db = None
try:
    creds_json_str = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if creds_json_str:
        creds_dict = json.loads(creds_json_str)
        cred = credentials.Certificate(creds_dict)
    else:
        cred_path = 'firebase-credentials.json'
        if os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
        else:
            print("AVISO: Credenciais do Firebase não encontradas.")
            cred = None
    
    if cred:
        # CORREÇÃO: Evita reinicializar a app Firebase
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("Firebase Admin SDK e Firestore inicializados com sucesso.")
except Exception as e:
    print(f"ERRO: Falha ao inicializar o Firebase Admin SDK: {e}")

def parse_data_file(file_content, icao_code):
    lines = file_content.split('\n')
    records = []
    for line in lines:
        line = line.strip()
        if not line or len(line) < 25 or re.search(r'\s+(MG|V)\s+\d{4}\s*$', line):
            continue
        record = {'timestamp': None, 'matricula': 'N/A', 'tipo_aeronave': 'N/A', 'origem': 'N/A', 'destino': 'N/A', 'regra_voo': 'N/A', 'pista': '', 'responsavel': 'N/A', 'flight_class': 'N/A', 'aerodromo': icao_code}
        try:
            date_str_header = line[9:15]
            data_block = line[15:].strip()
            match = None
            patterns = [
                r'^(?P<matricula>(?:AZU|GLO|TAM)\d{4})(?P<tipo_classe>[A-Z0-9]+[GSNM])\s+(?P<resto>.*)', r'^(?P<matricula>FAB\d+)(?P<tipo_classe>[A-Z0-9]+[GSNM])\s+(?P<resto>.*)',
                r'^(?P<matricula>N[A-Z0-9]+)(?P<tipo_classe>[A-Z0-9]+[GSNM])\s+(?P<resto>.*)', r'^(?P<matricula>\S+)\s+(?P<tipo>[A-Z0-9]+)\s+(?P<classe>[GSNM])\s+(?P<resto>.*)',
                r'^(?P<matricula>\S+)\s+(?P<tipo_classe>[A-Z0-9]+[GSNM])\s+(?P<resto>.*)',
            ]
            for pattern in patterns:
                match = re.match(pattern, data_block)
                if match: break
            if not match: continue
            g = match.groupdict()
            record['matricula'] = g['matricula']
            route_block = g['resto'].strip()
            if 'tipo_classe' in g:
                type_class_str = g['tipo_classe']
                record['tipo_aeronave'] = type_class_str[:-1]; record['flight_class'] = type_class_str[-1]
            else:
                record['tipo_aeronave'] = g['tipo']; record['flight_class'] = g['classe']
            op_match = re.search(r'\s([A-Z]{4})$', route_block)
            if op_match: record['responsavel'] = op_match.group(1); route_block = route_block[:op_match.start()].strip()
            pista_match = re.search(r'\s(07|25)$', route_block)
            if pista_match: record['pista'] = pista_match.group(1); route_block = route_block[:pista_match.start()].strip()
            rule_match = re.search(r'(IV|VV)', route_block)
            if rule_match: record['regra_voo'] = 'IFR' if rule_match.group(1) == 'IV' else 'VFR'
            horario_str = ''; waypoints = []
            overflight_match = re.search(r'([A-Z0-9]{4}).*?(\d{4}).*?([A-Z0-9]{4})', route_block)
            if overflight_match:
                waypoints.extend([overflight_match.group(1), overflight_match.group(3)]); horario_str = overflight_match.group(2)
            else:
                single_move_match = re.search(r'([A-Z0-9]{4}).*?(\d{4})', route_block)
                if single_move_match: waypoints.append(single_move_match.group(1)); horario_str = single_move_match.group(2)
                else:
                    time_only_match = re.search(r'(\d{4})', route_block)
                    if time_only_match: horario_str = time_only_match.group(1)
            if len(waypoints) >= 2: record['destino'], record['origem'] = waypoints[0], waypoints[1]
            elif len(waypoints) == 1: record['origem'], record['destino'] = icao_code, waypoints[0]
            else: record['origem'], record['destino'] = icao_code, icao_code
            if horario_str:
                dt_obj = datetime.strptime(f"{date_str_header}{horario_str}", '%d%m%y%H%M')
                record['timestamp'] = dt_obj.isoformat() + 'Z'
            records.append(record)
        except Exception as e:
            print(f"ERRO ao processar linha: '{line.strip()}'. Erro: {e}")
    return {"records": records}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/upload', methods=['POST'])
def upload_file():
    try:
        auth_header = request.headers.get('Authorization')
        id_token = auth_header.split(' ').pop()
        auth.verify_id_token(id_token)
    except Exception:
        return jsonify({"error": "Token inválido ou expirado"}), 401
    files = request.files.getlist('dataFiles')
    if not files or all(f.filename == '' for f in files):
        return jsonify({"error": "Nenhum ficheiro enviado"}), 400
    grouped_records = []
    for file in files:
        if file.filename != '':
            try:
                actual_filename = file.filename.split('/')[-1]
                icao_code_match = re.match(r'^([A-Z]{4})', actual_filename)
                if not icao_code_match:
                    print(f"AVISO: ICAO não determinado para '{actual_filename}'. A pular.")
                    continue
                icao_code = icao_code_match.group(1).upper()
                content = io.StringIO(file.stream.read().decode("utf-8", errors='ignore')).getvalue()
                parsed_data = parse_data_file(content, icao_code)
                if parsed_data["records"]:
                    grouped_records.append({"fileName": actual_filename, "records": parsed_data["records"]})
            except Exception as e:
                print(f"Erro ao processar {file.filename}: {e}")
                continue
    if not grouped_records:
        return jsonify({"error": "Nenhum registo válido encontrado nos ficheiros"}), 400
    return jsonify({"grouped_records": grouped_records})

@app.route('/api/save_records', methods=['POST'])
def save_records():
    try:
        auth_header = request.headers.get('Authorization')
        id_token = auth_header.split(' ').pop()
        decoded_token = auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
    except Exception:
        return jsonify({"error": "Token inválido ou expirado"}), 401

    if not db:
        return jsonify({"error": "Conexão com o banco de dados não disponível"}), 500
    
    try:
        payload = request.get_json()
        analysis_name = payload.get('analysisName')
        uploads_to_save = payload.get('uploadData')

        if not analysis_name or not uploads_to_save:
            return jsonify({"error": "Nome da análise e dados são obrigatórios"}), 400

        all_records_for_this_analysis = [record for group in uploads_to_save for record in group.get('records', [])]
        
        if not all_records_for_this_analysis:
            return jsonify({"error": "Nenhum registo para salvar"}), 400

        upload_ref = db.collection('flight_uploads').document()
        upload_ref.set({
            'userId': user_id,
            'createdAt': firestore.SERVER_TIMESTAMP,
            'analysisName': analysis_name,
            'recordCount': len(all_records_for_this_analysis)
        })
        
        batch = db.batch()
        for i, rec in enumerate(all_records_for_this_analysis):
            doc_ref = upload_ref.collection('records').document()
            batch.set(doc_ref, rec)
            if (i + 1) % 500 == 0:
                batch.commit()
                batch = db.batch()
        batch.commit()
        
        return jsonify({"success": True, "message": f"Análise '{analysis_name}' salva com sucesso!"}), 201
    except Exception as e:
        print(f"ERRO ao salvar no Firestore: {e}")
        return jsonify({"error": f"Erro interno ao salvar os dados: {str(e)}"}), 500

@app.route('/api/get_uploads', methods=['GET'])
def get_uploads():
    try:
        auth_header = request.headers.get('Authorization')
        id_token = auth_header.split(' ').pop()
        decoded_token = auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
    except Exception:
        return jsonify({"error": "Autenticação falhou"}), 401
    
    if not db:
        return jsonify([]), 200

    try:
        uploads_ref = db.collection('flight_uploads')
        query = uploads_ref.where('userId', '==', user_id).order_by('createdAt', direction=firestore.Query.DESCENDING)
        results = []
        for doc in query.stream():
            doc_data = doc.to_dict()
            results.append({
                'uploadId': doc.id,
                'recordCount': doc_data.get('recordCount'),
                'analysisName': doc_data.get('analysisName', 'Análise Sem Nome')
            })
        return jsonify(results), 200
    except Exception as e:
        print(f"ERRO ao buscar uploads: {e}")
        return jsonify({"error": "Não foi possível buscar o histórico de uploads."}), 500

@app.route('/api/get_records/<upload_id>', methods=['GET'])
def get_records(upload_id):
    try:
        auth_header = request.headers.get('Authorization')
        id_token = auth_header.split(' ').pop()
        decoded_token = auth.verify_id_token(id_token)
        user_id = decoded_token['uid']
    except Exception:
        return jsonify({"error": "Autenticação falhou"}), 401
    try:
        upload_doc = db.collection('flight_uploads').document(upload_id).get()
        if not upload_doc.exists or upload_doc.to_dict()['userId'] != user_id:
            return jsonify({"error": "Acesso não autorizado ou upload não encontrado"}), 403
        records_ref = db.collection('flight_uploads').document(upload_id).collection('records')
        records = [doc.to_dict() for doc in records_ref.stream()]
        return jsonify(records), 200
    except Exception as e:
        print(f"ERRO ao buscar registos do upload {upload_id}: {e}")
        return jsonify({"error": "Não foi possível buscar os registos."}), 500

if __name__ == '__main__':
    app.run(debug=True)

