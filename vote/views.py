import datetime
import json
import os
import platform
import random
import urllib.request

import bidi.algorithm
import cairosvg
from django.contrib.sessions.models import Session
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.template import Context, loader

from browser import html

from vote import models
from vote import render_content

dirname = os.path.dirname(__file__)

next_knesset = 20

def normalize_unicode(text):
    # this fixes party names so that names from 19th knesset and 18th match
    # ie ש”ס vs ש"ס
    return text.replace('"', '”').strip()

class CommonData:
    oknesset_path = dirname+'/data/oknesset/api/v2'
    party_long_and_short_names = [
      ('חזית דמוקרטית לשלום ושוויון', 'חד”ש'),
      ('ברית לאומית דמוקרטית', 'בל”ד'),
      ]

    def __init__(self):
        self.data = {}
    def __getitem__(self, key):
        if key not in self.data:
            self.data['key'] = getattr(self, 'load_'+key)()
        return self.data['key']
    def load_num_votes(self):
        return json.load(open(self.oknesset_path+'/vote/_limit=1')
            )['meta']['total_count']
    def load_members(self):
        return json.load(open(self.oknesset_path+'/member/_limit=0'))['objects']
    def load_member_of_id(self):
        return dict((x['id'], x) for x in self['members'])
    def load_party_of_member(self):
        parties = self['parties_by_id']
        result = {}
        for member in self['members']:
            party = parties[member['party_id']]
            result[member['id']] = party['name']
        return result
    def load_parties_by_id(self):
        parties = json.loads(open(
            self.oknesset_path+'/party/_knesset=all').read())['objects']
        for p in parties:
            p['name'] = normalize_unicode(p['name'])
            for long_name, short_name in self.party_long_and_short_names:
                if p['name'] == short_name:
                    p['name'] = long_name
                if p['name'] == long_name:
                    p['short_name'] = short_name
        return dict((x['id'], x) for x in parties)
    def load_parties(self):
        return dict(((p['name'], p['knesset_id']), p)
            for p in self['parties_by_id'].values())
    def load_parties_of_member(self):
        return json.loads(open(dirname+'/data/parties_of_member.json').read())
    def load_party_max_scores(self):
        return json.loads(open(dirname+'/data/party_max_scores.json').read())
    def load_parties_in_knesset(self):
        return json.loads(open(dirname+'/data/parties_in_knesset.json').read())

common_data = CommonData()

def home(request):
    state = request.session.get('state', {})
    prev_question_ids = [
        int(x[1:])
        for x in request.session.get('questions_order', [])
        if x.startswith('q')]
    prev_questions = [
        export_vote(v) for v in
        models.Vote.objects.filter(id__in=tuple(prev_question_ids))]
    # sort prev_questions by order
    prev_questions = dict((q['id'], q) for q in prev_questions)
    prev_questions = [prev_questions[qid] for qid in prev_question_ids]

    rendered_prevs_questions = []
    user_answers = {}
    for question in prev_questions:
        panel, party_votes_doc, radios = render_content.question_panel(question)
        answer = int(state['q%d'%question['id']])
        user_answers[question['id']] = answer
        for radio in radios:
            if int(radio.attrs['value']) == answer:
                radio.attrs['checked'] = 'true'
        render_content.question_party_votes(
            party_votes_doc, question, answer)
        rendered_prevs_questions.append(str(panel))
    results = render_content.calc_results(
        dict((q['id'], q) for q in prev_questions), user_answers)
    results_html = html.TBODY(id='results')
    small_results_html = html.DIV(id='results-small', style={'color': 'gray'})
    progress_html = html.DIV(id='progress-bar')
    render_content.render_results(
        results_html, small_results_html, progress_html, results, user_answers)

    start_votes = [
        x for x in
        models.Vote.objects.filter(is_interesting = True)
        if x.id not in prev_question_ids
        ]
    random.shuffle(start_votes)
    start_votes = [export_vote(x) for x in start_votes[:3]]

    if start_votes:
        question = start_votes.pop()
        prev_questions.append(question)
        panel, _, _ = render_content.question_panel(question)
        rendered_prevs_questions.append(str(panel))

    context = {
        'prev_questions': prev_questions,
        'prev_questions_html': '\n'.join(rendered_prevs_questions),
        'questions': start_votes,
        'results_html': results_html,
        'small_results_html': small_results_html,
        'progress_html': progress_html,
        }
    return render(request, 'vote/home.html', context)

def publish_data(votes_str):
    user_answers = {}
    vote_ids = []
    assert votes_str.startswith('q')
    for part in votes_str.split('q')[1:]:
        if part.endswith('f'):
            val = 1
        else:
            assert part.endswith('a')
            val = -1
        vote_id = int(part[:-1])
        vote_ids.append(vote_id)
        user_answers[vote_id] = val
    votes = {}
    for vote in models.Vote.objects.filter(id__in=tuple(vote_ids)):
        vote = export_vote(vote)
        votes[vote['id']] = vote

    questions = []
    for vote_id in vote_ids:
        vote = votes[vote_id]
        vote['answer'] = user_answers[vote_id]
        party_votes = html.DIV(Class='table-responsive')
        render_content.question_party_votes(party_votes, vote, vote['answer'])
        vote['party_votes_html'] = party_votes
        questions.append(vote)

    (results, _) = render_content.calc_results(votes, user_answers)

    return questions, results

def publish(request, votes_str):
    questions, results = publish_data(votes_str)
    context = {
        'questions': questions,
        'results_html': render_content.render_results_table(results),
        'url': request.get_host()+request.path,
        'share': request.GET.get('share', False),
        }
    return render(request, 'vote/publish.html', context)

def identity(x):
    return x

def publish_image_svg(votes_str, process_text=identity, text_anchor_start='start'):
    questions, results = publish_data(votes_str)
    for i, q in enumerate(questions):
        q['y'] = 55*i
        q['title'] = process_text(q['title'])
        q['color'] = '#69c134' if q['answer'] == 1 else '#fb001d'
    ordered_results = []
    for i, (pos, party_name, score) in enumerate(
        render_content.sorted_results(results)):
        ordered_results.append({
            'y': 15 + 45*i,
            'text': process_text('%d. %s' % (pos, party_name)),
            })
    logo = open(dirname+'/../vote_tool/static/logo.svg'
        ).read().split('<svg', 1)[1].split('>', 1)[1].rsplit('</svg>',1)[0]
    context = {
        'text_anchor_start': text_anchor_start,
        'my_results_text': process_text('התוצאות שלי:'),
        'questions': questions,
        'results': ordered_results,
        'logo': logo,
        'text_args':
            'font-family="arial"' if platform.system() == 'Darwin' else '',
        }
    return loader.get_template('vote/publish_image.svg').render(Context(context))

def publish_image(request, votes_str, extension):
    if extension == 'svg':
        svg = publish_image_svg(votes_str)
        return HttpResponse(svg, content_type='image/svg+xml')
    elif extension == 'png':
        svg = publish_image_svg(
            votes_str,
            process_text=bidi.algorithm.get_display,
            text_anchor_start='end',
            )
        return HttpResponse(cairosvg.svg2png(bytestring=svg), content_type='image/png')
    raise Http404

def track_changes(request):
    prev_state = request.session.get('state', {})
    if 's' in request.GET:
        t = [x.split(':') for x in request.GET['s'].split(',')]
        request.session['state'] = dict(t)
        request.session['questions_order'] = [x[0] for x in t]
    else:
        request.session['state'] = {}
        request.session['questions_order'] = []
    for k, v in request.session['state'].items():
        if v == prev_state.get(k):
            continue
        if k.startswith('q'):
            vote = models.Vote.objects.get(id=int(k[1:]))
        else:
            vote = None
        models.UserAnswer(
            session_key=request.session.session_key,
            vote=vote, answer=int(v)).save()

def fetch_vote(vote_id):
    try:
        vote = models.Vote.objects.get(id=vote_id)
    except models.Vote.DoesNotExist:
        # import vote from oknesset
        raw_json = urllib.request.urlopen(
            'https://oknesset.org/api/v2/vote/%d/' % vote_id).read()
        data = json.loads(raw_json.decode('utf8'))
        vote = models.Vote(
            id=vote_id, oknesset_data=raw_json,
            against_votes_count = data['against_votes_count'],
            for_votes_count = data['for_votes_count'],
            title = data['title'],
            )
        vote.save()
    return vote

def choose_question_set(already_asked):
    result = set()
    if random.random() < 0.95:
        result = set(
            x.id for x in
            models.Vote.objects.filter(is_interesting = True)
            ) - already_asked
    if not result and random.random() < 0.9:
        result = set(
            x.id for x in
            models.Vote.objects.filter(
                for_votes_count__gte = 10, against_votes_count__gte = 10)
            ) - already_asked
    if not result:
        result = set(range(1, common_data['num_votes']+1)) - already_asked
    return result

def knesset_of_vote(vote):
    vote_time = datetime.datetime.strptime(vote['time'], "%Y-%m-%dT%H:%M:%S" )
    for knesset_id, stop_time in [
            (18, datetime.datetime(2013, 1, 22)),
            (19, datetime.datetime(2015, 3, 18)),
            (20, None),
            ]:
        if vote_time < stop_time:
            break
    return knesset_id

def calc_party_votes(vote):
    result = {}
    member_of_id = common_data['member_of_id']
    parties_of_member = common_data['parties_of_member']
    vote_knesset_id = vote['knesset_id']

    for party_name in common_data['parties_in_knesset'][str(vote_knesset_id)]:
        result[party_name] = {}

    for member_vote in vote['votes']:
        def id_from_uri(uri):
            return int(uri.rstrip('/').rsplit('/', 1)[1])
        member = member_of_id[id_from_uri(member_vote['member'])]
        member_name = member['name']
        if member_name not in parties_of_member:
            # There are members, such as
            # יואב בן צור
            # which entered the knesset for short periods after other members
            # quit their parties.
            # Specifically as Shas members resigned
            # at the very end of 19th Knesset.
            # We ignore these cases as
            # it would be complicated to account for it.
            # TODO: fix this? or is this negligible anyhow?
            continue
        member_parties = parties_of_member[member_name]
        if member_name not in parties_of_member:
            # from old party that doesn't exist
            continue
        member_vote_type = member_vote['vote_type']
        for knesset_id in [vote_knesset_id, next_knesset]:
            knesset_id_str = str(knesset_id)
            if knesset_id_str not in member_parties:
                continue
            party_name = member_parties[knesset_id_str]
            party_res = result[party_name]
            party_res[member_vote_type] = 1 + party_res.get(member_vote_type, 0)
    party_max_scores = common_data['party_max_scores']
    for party_name, party_res in result.items():
        for k in party_res.keys():
            party_res[k] /= party_max_scores[party_name][str(vote_knesset_id)]
    return result

def export_vote(vote):
    vote_raw_json = bytes(vote.oknesset_data).decode('utf8')
    vote_json = json.loads(vote_raw_json)
    if vote.vt_title:
        vote_json['title'] = vote.vt_title
    else:
        title = vote_json['title']
        for prefix in [
            'להעביר את הצעת החוק לוועדה - ',
            'להעביר את הצעת החוק לוועדה שתקבע ועדת הכנסת - ',
            'הצבעה -',
            ]:
            if title.startswith(prefix):
                vote_json['title'] = vote_json['title'][len(prefix):]
                break
    if vote.vt_description:
        vote_json['summary'] = vote.vt_description
    vote_json['knesset_id'] = knesset_of_vote(vote_json)
    vote_json['party_votes'] = calc_party_votes(vote_json)
    return vote_json

def get_specific_question(request, question_id = None):
    return HttpResponse(json.dumps(
        export_vote(fetch_vote(int(question_id))),
        ensure_ascii=False))

def get_question(request):
    track_changes(request)

    already_asked = list(request.session['state'].keys())
    queue = request.GET.get('queue', '')
    if queue:
        already_asked += queue.split(',')
    already_asked = set( int(x[1:]) for x in already_asked if x.startswith('q'))

    question_set = choose_question_set(already_asked)
    question_id = random.choice(list(question_set))
    return HttpResponse(
        json.dumps(export_vote(fetch_vote(question_id)), ensure_ascii=False))

def save_vote(request):
    track_changes(request)
    return HttpResponse('ok')

def restart(request):
    request.session['state'] = {}
    request.session['questions_order'] = []
    return HttpResponseRedirect('/')
