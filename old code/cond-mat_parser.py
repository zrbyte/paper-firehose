# Standard library imports
import os
import pickle, re, datetime, shutil
import feedparser
# import pysftp
import ftplib
import logging
import sys

## setup ----------------------------------
searchterms_all = '(topolog[a-z]+)|(graphit[a-z]+)|(rhombohedr[a-z]+)|(graphe[a-z]+)|(chalcog[a-z]+)|(landau)|(weyl)|(dirac)|(STM)|(scan[a-z]+ tunne[a-z]+ micr[a-z]+)|(scan[a-z]+ tunne[a-z]+ spectr[a-z]+)|(scan[a-z]+ prob[a-z]+ micr[a-z]+)|(MoS.+\d+|MoS\d+)|(MoSe.+\d+|MoSe\d+)|(MoTe.+\d+|MoTe\d+)|(WS.+\d+|WS\d+)|(WSe.+\d+|WSe\d+)|(WTe.+\d+|WTe\d+)|(Bi\d+Rh\d+I\d+|Bi.+\d+.+Rh.+\d+.+I.+\d+.+)|(BiTeI)|(BiTeBr)|(BiTeCl)|(ZrTe5|ZrTe.+5)|(Pt2HgSe3|Pt.+2HgSe.+3)|(jacuting[a-z]+)'
# searchterms_all = '[a-z]+'
searchterms_graphene = '(graphit[a-z]+)|(graphe[a-z]+)|(STM)|(scann[a-z]+ tunnel[a-z]+ microsco[a-z]+)|(scann[a-z]+ tunnel[a-z]+ spectr[a-z]+)|(scann[a-z]+ tunnel[a-z]+ micr[a-z]+)'
searchterms_tmdc = '(chalcog[a-z]+)|(tmdc)|(MoS.+\d+|MoS\d+)|(MoSe.+\d+|MoSe\d+)|(MoTe.+\d+|MoTe\d+)|(WS.+\d+|WS\d+)|(WSe.+\d+|WSe\d+)|(WTe.+\d+|WTe\d+)|(STM)|(scann[a-z]+ tunnel[a-z]+ microsco[a-z]+)|(scann[a-z]+ tunnel[a-z]+ spectr[a-z]+)|(scann[a-z]+ tunnel[a-z]+ micr[a-z]+)'

database = {
	'cond-mat': 'https://rss.arxiv.org/rss/cond-mat',
	'prb': 'http://feeds.aps.org/rss/recent/prb.xml',
	'prl': 'http://feeds.aps.org/rss/recent/prl.xml',
	'prx': 'http://feeds.aps.org/rss/recent/prx.xml',
	'pr_res': 'http://feeds.aps.org/rss/recent/prresearch.xml',
	'nano-lett': 'https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=nalefd',
	'acs-nano': 'https://pubs.acs.org/action/showFeed?type=axatoc&feed=rss&jc=ancac3',
	'science-adv': 'https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv',
	'sci-rep': 'http://feeds.nature.com/srep/rss/current',
	'nat-comm': 'https://www.nature.com/subjects/physical-sciences/ncomms.rss',
	'comm-phys': 'http://feeds.nature.com/commsphys/rss/current',
	'research': 'https://spj.sciencemag.org/journals/research/rss/',
	'scipost': 'https://scipost.org/rss/submissions/',
	'small': 'https://onlinelibrary.wiley.com/feed/16136829/most-recent',
	'adv-mater': 'https://onlinelibrary.wiley.com/feed/15214095/most-recent'
	}
feeds = [
	'cond-mat',
	'prb',
	'prl',
	'prx',
	'pr_res',
	'nano-lett',
	'acs-nano',
	'science-adv',
	'sci-rep',
	'nat-comm',
	'comm-phys',
	# 'research',
	'scipost',
	'small',
	'adv-mater'
	]

today = datetime.date.today()
## regular expressions used 
re_id = re.compile('\d\d\d\d.\d+')

## search term to be used in title and abstract
re_search_all = re.compile(searchterms_all, re.IGNORECASE)
re_search_graph = re.compile(searchterms_graphene, re.IGNORECASE)
re_search_tmdc = re.compile(searchterms_tmdc, re.IGNORECASE)


## ----------------------------------------

## functions
def get_results(feed, re_search):
	content = feedparser.parse(database[feed])
	feedlength = len(content.entries) - 1

	## save feed data to file ------------
	# filename = '/uu/nemes/cond-mat/pickle/' + str(today) + '_' + feed + '.uborka'
	# pickle.dump(content, open(filename, 'wb'))
	## read from pickle file
	# content2 = pickle.load(open(filename, 'rb'))

	# make files for the first time if they do not exist
	if not os.path.exists('id_' + feed + '.txt'):
		with open('id_' + feed + '.txt', 'w') as f:
			f.write(str(today))
			f.write('\n')

	## read the previous days entries from txt file
	with open('id_' + feed + '.txt', 'r') as f:
		previousentries = f.readlines()

	## read the run ID of the ID file, which is just the date
	run_date = previousentries[0]

	## if the previous days entries are form today, disregard it, else compare with the previous day
	matches = []
	if str(run_date)[:-1] == str(today):
		## search for regexp in title and summary
		for entry in content.entries:
			if (re_search.search(entry.title)) or (re_search.search(entry.summary)):
				matches += [content.entries.index(entry)]
	else:
	## iterate through the entries in content
		for entry in content.entries:
			## only search if the entry id is not in the previous days list
			if entry.id + '\n' not in previousentries:
				## search for regexp in title and summary
				if (re_search.search(entry.title)) or (re_search.search(entry.summary)):
					matches += [content.entries.index(entry)]

	## write data to a dictionary
	result = {}
	for i in matches:
		try:
			result[i] = [content.entries[i].title]
		except:
			result[i] = ['< title missing >']
		try:
			result[i] += [content.entries[i].author]
		except:
			result[i] += ['< author missing >']
		try:
			result[i] += [content.entries[i].summary]
		except:
			result[i] += ['< summary missing >']
		try:
			result[i] += [content.entries[i].link]
		except:
			result[i] += ['< link missing >']
	print('found', len(matches), 'papers in', feed)

	## save the ID of all entries, to be compared with tomorrows
	with open('id_' + feed + '.txt', 'w') as f:
		## write the current date for future comparison
		f.write(str(today))
		f.write('\n')
		for entry in content.entries:
			f.write(entry.id)
			f.write('\n')
	## copy files into a separate directory
	shutil.copy2('id_' + feed + '.txt', './archive/' + 'id_' + feed + str(today) + '.txt')

	return content, result, matches

def html_generate(content, result, matches, searchterms):
	## create outputext
	outputtext = '''<html> <head></head> <body> <p> <font face="helvetica">'''
	outputtext += '''Found ''' + str(len(matches)) + ''' papers in ''' + feed
	outputtext += '''<br>'''
	try:
		outputtext += '''Date of feed: ''' + content.modified
	except AttributeError:
		pass
	outputtext += '''<br>'''
	outputtext += '''<br>'''
	outputtext += '''<b>Search terms: </b>'''
	outputtext += searchterms
	outputtext += '''<br>'''
	outputtext += '''<br>'''
	## write results to table in a html file
	for i in result:
		outputtext += '''<table border = "0" width = "60%">'''
		outputtext += '''<tr> <td>'''
		outputtext += '''<p>''' + '''<a href = ' ''' + result[i][3] + ''' '> <b>''' + result[i][0] + '''</b>''' + '''</a>'''
		outputtext += '''<br>''' + result[i][1] + '''<br>'''
		outputtext += result[i][2]
		outputtext += '''<br> </td> </tr>'''
		outputtext += '''</table>'''
	outputtext += '''</font></p></body> </html>'''

	return outputtext


## ------------------------------------------------------------

# feeds = []
# for key in database.keys():
# 	feeds += [key]

## iterate through feeds -----------------
outputtext_all = ''
outputtext_graph = ''
outputtext_tmdc = ''
for feed in feeds:
	content, result, matches = get_results(feed, re_search_all)
	if len(matches) != 0:
		outputtext_all += html_generate(content, result, matches, searchterms_all)


	# content, result, matches = get_results(feed, re_search_graph)
	# outputtext_graph += html_generate(content, result, matches, searchterms_graphene)

	# content, result, matches = get_results(feed, re_search_tmdc)
	# outputtext_tmdc += html_generate(content, result, matches, searchterms_tmdc)

outputfile_all = 'result_all.html'
outputfile_all_archive = 'result_all_' + str(today) + '.html'
outputfile_graph = 'result_graphene.html'
outputfile_tmdc = 'result_tmdc.html'

with open(outputfile_all, 'w', encoding='utf-8') as f:
	f.write(outputtext_all)
with open('./archive/' + outputfile_all_archive, 'w', encoding = 'utf-8') as f:
	f.write(outputtext_all)

## write to nemeslab.com
<<<<<<< HEAD:old code/cond-mat_parser.py
# with ftplib.FTP('nemeslab.com') as session:
# 	session.login(user = 'nemeslab', passwd = 'zzzzz')
# 	session.cwd('/public_html/cond-mat/')
# 	with open(outputfile_all, 'rb') as f:
# 		session.storbinary('STOR ' + outputfile_all, f)
# 	session.cwd('/public_html/wp-content/uploads/simple-file-list/')
# 	with open(outputfile_all, 'rb') as f:
# 		session.storbinary('STOR ' + outputfile_all_archive, f)
=======
try:
        with ftplib.FTP('nemeslab.com') as session:
                session.login(user = 'nemeslab', passwd = 'x01w35lFYq')
                session.cwd('/public_html/cond-mat/')
                with open('/uu/nemes/cond-mat/' + outputfile_all, 'rb') as f:
                        session.storbinary('STOR ' + outputfile_all, f)
                with open('/uu/nemes/cond-mat/result_rg.html', 'rb') as f:
                        session.storbinary('STOR ' + 'result_rg.html', f)
                session.cwd('/public_html/wp-content/uploads/simple-file-list/')
                with open('/uu/nemes/cond-mat/archive/' + outputfile_all_archive, 'rb') as f:
                        session.storbinary('STOR ' + outputfile_all_archive, f)
except ftplib.all_errors as e:
        logging.error("FTP upload failed: %s", e)
        sys.exit(1)
>>>>>>> 5cab174f71652e5d0d82800a6996cc242694a64f:server version/cond-mat_parser.py

## write to public.ek-cer.hu
# cnopts = pysftp.CnOpts()
# cnopts.hostkeys = None  
# with pysftp.Connection('public.ek-cer.hu', username = 'nemes', password = 'ellerium137Fhtagn?', cnopts = cnopts) as sftp:
# 	with sftp.cd('/uu/.public_html/nemes'):
# 		sftp.put(outputfile_all)

# with open(outputfile_graph_archive, 'w') as f:
# 	f.write(outputtext_graph)
# with open(outputfile_graph, 'w', encoding='utf-8') as f:
# 	f.write(outputtext_graph)

# with open(outputfile_tmdc_archive, 'w') as f:
# 	f.write(outputtext_tmdc)
# with open(outputfile_tmdc, 'w', encoding='utf-8') as f:
# 	f.write(outputtext_tmdc)
